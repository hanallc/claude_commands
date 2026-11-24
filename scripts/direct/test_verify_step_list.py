"""Exercise the Verify step list with private routing and Cargo fixtures."""
from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import subprocess
from typing import cast
import unittest
from unittest import mock

from scripts.build_queue import admission_client, admission_protocol as wire, admission_records as records, admission_service as service
from scripts.build_queue import placement
from scripts.build_queue.admission_test_support import caller_admission, wait_reply
from scripts.build_targets import target_config
from scripts.direct.verify_step_routes_test_support import VerifyStepRoutesFixture
from scripts.lib.build_folders_test_support import tool_folders
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class VerifyStepListTests(VerifyStepRoutesFixture):
    @contextmanager
    def capture_placement_requests(self) -> Generator[dict[str, wire.PlacementRequest]]:
        """Record the first placement request for each episode."""
        admission = caller_admission(self.fixture, self.fixture.root, self.fixture.environment)
        previous = admission.server.handle_request
        requests: dict[str, wire.PlacementRequest] = {}

        def handle(request: wire.Request, connection: service.Connection | service.NoConnection = service.NO_CONNECTION
                   ) -> list[wire.Reply]:
            if isinstance(request, wire.PlacementRequest):
                _ = requests.setdefault(request.episode, request)
            return previous(request, connection)

        with mock.patch.object(admission.server, "handle_request", side_effect=handle):
            yield requests

    def test_steps_keep_order_arguments_and_run_identity(self) -> None:
        root = self.fixture.root
        (root / "other/src").mkdir(parents=True)
        _ = (root / "other/Cargo.toml").write_text(
            '[package]\nname = "other"\nversion = "0.1.0"\n[features]\nextra = []\n')
        _ = (root / "other/src/lib.rs").write_text("pub fn other() {}\n")
        with (root / "Cargo.toml").open("a") as manifest:
            _ = manifest.write('\n[workspace]\nmembers = ["other"]\n')
        self.prepare_repository("other")
        metadata = cast(dict[str, object], json.loads(self.fixture.environment["TEST_METADATA"]))
        cast(list[str], metadata["workspace_members"]).append("other-id")
        cast(list[dict[str, object]], metadata["packages"]).append({
            "name": "other", "id": "other-id", "features": {"extra": []}, "dependencies": [],
            "manifest_path": str(root / "other/Cargo.toml"),
            "targets": [{"name": "other", "kind": ["lib"], "test": True,
                         "src_path": str(root / "other/src/lib.rs")}],
        })
        self.fixture.environment["TEST_METADATA"] = json.dumps(metadata)
        self.fixture.environment["BUILDLOG_OFF"] = "0"
        (Path(self.fixture.environment["BUILD_HOLD_DIR"]) / "other").unlink()
        with self.capture_placement_requests() as requests:
            result = self.run_verify(
                "check", "sample", "--local", "requires Linux kernel", "--features", "extra", "+",
                "lint", "--check", "+",
                "test", "sample", "--target", "natedev", "--filter", "exact", "+",
                "test", "other", "--local", "requires Linux kernel", "--features", "extra", "+",
                "doc", "--mac", "Mac-only docs + Swift", "--features", "extra",
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = cast(list[dict[str, object]], self.fixture.call_records())
        self.assertEqual([(row["verb"], row["package"]) for row in rows],
                         [("check", "sample"), ("lint", "workspace"), ("test", "sample"),
                          ("test", "other"), ("doc", "workspace")])
        self.assertEqual(len({row["id"] for row in rows}), 5)
        placements = list(requests.values())
        run = placements[0].run
        self.assertIsInstance(run, wire.RunIdentity)
        assert isinstance(run, wire.RunIdentity)
        self.assertEqual([row["run_id"] for row in rows], [f"{run.pid}:{run.start_time}"] * 5)
        self.assertEqual([request.run for request in placements], [run] * 6)
        routes = list(dict.fromkeys((request.identity.step, request.route) for request in placements))
        self.assertEqual(routes, [
            ("check", target_config.LocalRequired("requires Linux kernel")),
            ("mend", target_config.ANY_ELIGIBLE_TARGET),
            ("clippy", target_config.ANY_ELIGIBLE_TARGET),
            ("nextest", target_config.PinnedTarget("natedev")),
            ("nextest", target_config.LocalRequired("requires Linux kernel")),
            ("doc", target_config.MAC_ONLY),
        ])
        local_calls = [event["args"] for event in self.fixture.cargo_events()]
        self.assertIn(["check", "--workspace", "--all-targets", "--features", "sample/extra"], local_calls)
        test_calls = [event["args"] for event in self.fixture.local_runs()]
        self.assertEqual(len(test_calls), 2)
        self.assertIn("package(sample) & test(exact)", test_calls[0])
        self.assertIn("package(other)", test_calls[1])
        self.assertIn("other/extra", test_calls[1])
        remote_verbs = [cast(list[str], row["args"]) for row in self.mac_rows()]
        doc = remote_verbs[-1]
        self.assertEqual(doc[doc.index("--verb") + 1], "doc")
        self.assertIn("extra", doc)
        self.assertIn("Mac only (Mac-only docs + Swift)", result.stderr)
        self.assert_removed()

    def test_comma_steps_keep_one_run_identity_in_a_slot_copy(self) -> None:
        self.prepare_repository()
        root = self.fixture.root
        _ = subprocess.run(["git", "remote", "add", "origin", str(root)], cwd=root,
                           env=self.fixture.environment, check=True, capture_output=True, timeout=TEST_HANG_GUARD_SECS)
        self.fixture.environment.update({"BUILDLOG_OFF": "0", "COPIES_STEP_SOURCE": str(root),
                                         "COPIES_STEP_OWNER": "fixture", "COPIES_STEP_POOL": str(root)})
        (Path(self.fixture.environment["BUILD_HOLD_DIR"]) / "other").unlink()
        with self.capture_placement_requests() as requests:
            result = self.run_verify("lint,test,doc", "sample")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = cast(list[dict[str, object]], self.fixture.call_records())
        self.assertEqual([row["verb"] for row in rows], ["lint", "test", "doc"])
        self.assertEqual(len({row["id"] for row in rows}), 3)
        placements = list(requests.values())
        self.assertEqual([request.identity.step for request in placements], ["mend", "clippy", "nextest", "doc"])
        run = placements[0].run
        self.assertIsInstance(run, wire.RunIdentity)
        assert isinstance(run, wire.RunIdentity)
        self.assertEqual([row["run_id"] for row in rows], [f"{run.pid}:{run.start_time}"] * 3)
        self.assertEqual([request.run for request in placements], [run] * 4)
        self.assert_removed()

    def test_each_call_records_its_admission_wait_under_the_outer_run(self) -> None:
        self.prepare_repository()
        root = self.fixture.root
        startup = root / "verify-clock.sh"
        _ = startup.write_text('''if [[ ${0##*/} == verify.sh ]]; then
    unset EPOCHREALTIME
    EPOCHREALTIME="$(date +%s).000000"
fi
''')
        token_clock = root / "token-clock"
        _ = token_clock.write_text("0\n")
        self.fixture.environment.update({"BUILDLOG_OFF": "0", "BASH_ENV": str(startup),
                                         "UNIT_TOKEN_CLOCK_FILE": str(token_clock)})
        (Path(self.fixture.environment["BUILD_HOLD_DIR"]) / "other").unlink()
        admission = caller_admission(self.fixture, root, self.fixture.environment)
        previous = admission.server.handle_request
        observed: dict[str, wire.PlacementRequest] = {}
        canceled = False
        after_format = False
        wait = wait_reply(records.NeedsMemory())
        taplo_calls = root / "taplo-calls"
        self.fixture.environment["TEST_TAPLO_CALLS"] = str(taplo_calls)
        self.fixture.write_executable(self.fixture.bin_directory / "taplo", """import os
from pathlib import Path
import sys
if sys.argv[1:] == ["--version"]:
    print("taplo fixture 1.0")
else:
    Path(os.environ["TEST_TAPLO_CALLS"]).touch()
""")
        waits = {"mend": 4200, "clippy": 720, "nextest": 1290, "doc": 420}

        def handle(request: wire.Request, connection: service.Connection | service.NoConnection = service.NO_CONNECTION
                   ) -> list[wire.Reply]:
            nonlocal canceled, after_format
            if isinstance(request, wire.CancelPlacement) and request.reason == "judge lint before local repair":
                folder, = tool_folders(root / "tmp", "verify", pid=request.owner.pid)
                path = folder / f"placement-{request.episode}.json.wait"
                saved = wire.parse_record(path.read_text(), admission_client.PlacementWaitNotice)
                assert saved.sampled_at is not None
                # Advance the time outside the queue once the first sample is complete.
                _ = path.write_text(wire.encode_record(replace(
                    saved, started=saved.started - 600, sampled_at=saved.sampled_at - 600)))
                canceled = True
                return [wire.Ok()]
            if isinstance(request, wire.PrerequisiteReport) and not canceled and "mend" in observed:
                if request.episode == observed["mend"].episode:
                    return [wire.Ok()]
            if isinstance(request, wire.PlacementRequest) and request.identity.step in waits:
                step = request.identity.step
                candidates = tuple(placement.CandidateEligibility(candidate.destination, candidate.eligibility)
                                   for candidate in request.constraints.candidates)
                if step not in observed:
                    observed[step] = request
                    folder, = tool_folders(root / "tmp", "verify", pid=request.owner.pid)
                    path = folder / f"placement-{request.episode}.json.wait"
                    notice = admission_client.PlacementWaitNotice(0, (), 1,
                        wire.PlacementResourceWait("mac", "busy"), waits[step])
                    _ = path.write_text(wire.encode_record(notice))
                    eligible = candidates if step == "mend" else ()
                    return [wire.PlacementWait(request.episode, request.since, 0, eligible)]
                if step == "mend" and not canceled:
                    return [wire.PlacementWait(request.episode, request.since, 1, candidates, wait)]
                if step == "mend":
                    after_format = (taplo_calls.is_file() and any("fmt" in event["args"]
                        and "--version" not in event["args"] for event in self.fixture.cargo_events()))
            return previous(request, connection)

        with mock.patch.object(admission.server, "handle_request", side_effect=handle):
            result = self.run_verify("lint", "sample", "+", "test", "sample", "+",
                                     "doc", "sample", "--mac", "Mac-only docs")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(canceled)
        self.assertTrue(after_format)
        self.assertEqual([request.identity.step for request in observed.values()], ["mend", "clippy", "nextest", "doc"])
        rows = cast(list[dict[str, object]], self.fixture.call_records())
        run = next(iter(observed.values())).run
        assert isinstance(run, wire.RunIdentity)
        self.assertEqual([(row["verb"], row["admission_wait_s"], row["run_id"]) for row in rows],
                         [("lint", 4920, f"{run.pid}:{run.start_time}"), ("test", 1290, f"{run.pid}:{run.start_time}"),
                          ("doc", 420, f"{run.pid}:{run.start_time}")])
        lint_waits = cast(dict[str, int], rows[0]["step_wait_s"])
        self.assertEqual((lint_waits["mend"], lint_waits["clippy"]), (4200, 720))
        self.assertEqual([row["step_wait_s"] for row in rows[1:]], [{"test": 1290}, {"doc": 420}])

    def test_mend_cancellation_records_the_wait_when_taplo_fails(self) -> None:
        self.prepare_repository()
        root = self.fixture.root
        startup = root / "verify-clock.sh"
        _ = startup.write_text('''if [[ ${0##*/} == verify.sh ]]; then
    unset EPOCHREALTIME
    EPOCHREALTIME="$(date +%s).000000"
fi
''')
        self.fixture.environment.update({"BUILDLOG_OFF": "0", "BASH_ENV": str(startup)})
        (Path(self.fixture.environment["BUILD_HOLD_DIR"]) / "other").unlink()
        canceled = root / "mend-canceled"
        self.fixture.write_executable(self.fixture.bin_directory / "taplo", f"""from pathlib import Path
import sys
if sys.argv[1:] == ["--version"]:
    print("taplo fixture 1.0")
elif Path({str(canceled)!r}).is_file():
    sys.exit(9)
""")
        admission = caller_admission(self.fixture, root, self.fixture.environment)
        previous = admission.server.handle_request
        episode = ""
        wait = wait_reply(records.NeedsMemory())

        def handle(request: wire.Request, connection: service.Connection | service.NoConnection = service.NO_CONNECTION
                   ) -> list[wire.Reply]:
            nonlocal episode
            if isinstance(request, wire.PlacementRequest) and request.identity.step == "mend":
                candidates = tuple(placement.CandidateEligibility(candidate.destination, candidate.eligibility)
                                   for candidate in request.constraints.candidates)
                if not episode:
                    episode = request.episode
                    folder, = tool_folders(root / "tmp", "verify", pid=request.owner.pid)
                    path = folder / f"placement-{episode}.json.wait"
                    notice = admission_client.PlacementWaitNotice(0, (), 1,
                        wire.PlacementResourceWait("mac", "busy"), 4)
                    _ = path.write_text(wire.encode_record(notice))
                    return [wire.PlacementWait(episode, request.since, 0, candidates)]
                return [wire.PlacementWait(request.episode, request.since, 1, candidates, wait)]
            if isinstance(request, wire.PrerequisiteReport) and request.episode == episode:
                return [wire.Ok()]
            if (isinstance(request, wire.CancelPlacement) and request.episode == episode
                    and request.reason == "judge lint before local repair"):
                canceled.touch()
                return [wire.Ok()]
            return previous(request, connection)

        with mock.patch.object(admission.server, "handle_request", side_effect=handle):
            result = self.run_verify("lint", "sample")
        self.assertEqual(result.returncode, 9, result.stdout + result.stderr)
        self.assertTrue(canceled.is_file())
        row, = cast(list[dict[str, object]], self.fixture.call_records())
        self.assertEqual(row["step_wait_s"], {"fmt": 0, "mend": 4})
        self.assertEqual(row["admission_wait_s"], 4)
        self.assert_removed()

    def test_confirmed_placement_cancellation_records_the_memory_wait(self) -> None:
        self.prepare_repository()
        root = self.fixture.root
        startup = root / "verify-clock.sh"
        _ = startup.write_text('''if [[ ${0##*/} == verify.sh ]]; then
    unset EPOCHREALTIME
    EPOCHREALTIME="$(date +%s).000000"
fi
''')
        self.fixture.environment.update({"BUILDLOG_OFF": "0", "BASH_ENV": str(startup),
                                         "COPIES_STEP_SOURCE": str(root),
                                         "LINT_QUEUE_LEAVE_HOOK": "test -f leave-admission"})
        (Path(self.fixture.environment["BUILD_HOLD_DIR"]) / "other").unlink()
        admission = caller_admission(self.fixture, root, self.fixture.environment)
        previous = admission.server.handle_request
        episodes: list[str] = []
        canceled: list[str] = []

        def handle(request: wire.Request, connection: service.Connection | service.NoConnection = service.NO_CONNECTION
                   ) -> list[wire.Reply]:
            if isinstance(request, wire.PlacementRequest):
                if not episodes:
                    folder, = tool_folders(root / "tmp", "verify", pid=request.owner.pid)
                    path = folder / f"placement-{request.episode}.json.wait"
                    notice = admission_client.PlacementWaitNotice(0, (), 1,
                        wire.PlacementResourceWait("mac", "busy"), 4)
                    _ = path.write_text(wire.encode_record(notice))
                else:
                    (root / "leave-admission").touch()
                episodes.append(request.episode)
                return [wire.PlacementWait(request.episode, request.since, 1, ())]
            if isinstance(request, wire.CancelPlacement) and request.reason == "the caller left before placement":
                canceled.append(request.episode)
                return [wire.Ok()]
            return previous(request, connection)

        with mock.patch.object(admission.server, "handle_request", side_effect=handle):
            result = self.run_verify("check", "sample", "--local", "requires Linux")
        self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
        episode, = canceled
        self.assertEqual(episodes, [episode, episode])
        row, = cast(list[dict[str, object]], self.fixture.call_records())
        self.assertEqual(row["step_wait_s"], {"check": 4})
        self.assertEqual(row["admission_wait_s"], 4)
        self.assert_removed()

    def test_term_marks_the_unfinished_admission_call_as_not_measured(self) -> None:
        self.prepare_repository()
        root = self.fixture.root
        self.fixture.environment["BUILDLOG_OFF"] = "0"
        (Path(self.fixture.environment["BUILD_HOLD_DIR"]) / "other").unlink()
        admission = caller_admission(self.fixture, root, self.fixture.environment)
        previous = admission.server.handle_request
        wait = wait_reply(records.NeedsMemory())
        requests = 0

        def handle(request: wire.Request, connection: service.Connection | service.NoConnection = service.NO_CONNECTION
                   ) -> list[wire.Reply]:
            nonlocal requests
            if isinstance(request, wire.PrerequisiteReport):
                return [wire.Ok()]
            if not isinstance(request, wire.PlacementRequest):
                return previous(request, connection)
            requests += 1
            if requests == 2:
                os.kill(request.owner.pid, signal.SIGTERM)
                return []
            candidates = tuple(placement.CandidateEligibility(candidate.destination, candidate.eligibility)
                               for candidate in request.constraints.candidates)
            return [wire.PlacementWait(request.episode, request.since, 1, candidates, wait)]

        with mock.patch.object(admission.server, "handle_request", side_effect=handle):
            result = self.run_verify("check", "sample", "--local", "requires Linux")
        self.assertEqual(result.returncode, 143, result.stdout + result.stderr)
        self.assertEqual(requests, 2)
        row, = cast(list[dict[str, object]], self.fixture.call_records())
        self.assertEqual(row["outcome"], "interrupted")
        self.assertEqual(row["step_wait_s"], {"check": None})
        self.assertIsNone(row["admission_wait_s"])
        self.assertTrue(all(event["args"][:1] == ["metadata"] for event in self.fixture.cargo_events()))
        self.assert_removed()

    def test_term_during_the_memory_poll_marks_the_wait_as_not_measured(self) -> None:
        root = self.fixture.root
        waiter = root / "interrupt-poll.py"
        _ = waiter.write_text("""\
import os
from pathlib import Path
import signal

directory = Path(os.environ["BUILDLOG_HOLD_WAIT_FILE"]).parent
sample, = directory.glob("placement-call-*.pending/sample-*")
elapsed = int(sample.read_text())
owner = int(Path("poll-owner").read_text())
assert os.getppid() == owner
Path("poll-sample").write_text(str(elapsed))
os.kill(owner, signal.SIGTERM)
""")
        self.fixture.environment["VERIFY_LOCAL_RELEASE_WAIT_SCRIPT"] = str(waiter)
        self.check_term_with_unfinished_memory_sample()

    def test_term_before_verify_reads_the_memory_wait_reply(self) -> None:
        startup = self.fixture.root / "interrupt-placement.sh"
        _ = startup.write_text('''\
if [[ ${0##*/} == verify.sh ]]; then
    wait() {
        local status=0
        builtin wait "$@" || status=$?
        if [[ ${PLACEMENT_CLIENT_ACTIVE:-0} == 1 && $1 == "$HOLD_WAITER_PID" && $status == 78 ]]; then
            local sample
            IFS= read -r sample < "$wait_result" || return 2
            printf '%s\\n' "$sample" > poll-sample
            kill -s TERM "$BASHPID"
        fi
        return "$status"
    }
fi
''')
        self.fixture.environment["BASH_ENV"] = str(startup)
        self.check_term_with_unfinished_memory_sample()

    def check_term_with_unfinished_memory_sample(self) -> None:
        self.prepare_repository()
        root = self.fixture.root
        self.fixture.environment["BUILDLOG_OFF"] = "0"
        (Path(self.fixture.environment["BUILD_HOLD_DIR"]) / "other").unlink()
        admission = caller_admission(self.fixture, root, self.fixture.environment)
        previous = admission.server.handle_request
        wait = wait_reply(records.NeedsMemory())

        def handle(request: wire.Request, connection: service.Connection | service.NoConnection = service.NO_CONNECTION
                   ) -> list[wire.Reply]:
            if isinstance(request, wire.PrerequisiteReport):
                return [wire.Ok()]
            if not isinstance(request, wire.PlacementRequest):
                return previous(request, connection)
            _ = (root / "poll-owner").write_text(str(request.owner.pid))
            candidates = tuple(placement.CandidateEligibility(candidate.destination, candidate.eligibility)
                               for candidate in request.constraints.candidates)
            return [wire.PlacementWait(request.episode, request.since, 1, candidates, wait)]

        with mock.patch.object(admission.server, "handle_request", side_effect=handle):
            result = self.run_verify("check", "sample", "--local", "requires Linux")
        self.assertEqual(result.returncode, 143, result.stdout + result.stderr)
        self.assertGreaterEqual(int((root / "poll-sample").read_text()), 0)
        row, = cast(list[dict[str, object]], self.fixture.call_records())
        self.assertEqual(row["outcome"], "interrupted")
        self.assertEqual(row["step_wait_s"], {"check": None})
        self.assertIsNone(row["admission_wait_s"])
        self.assertTrue(all(event["args"][:1] == ["metadata"] for event in self.fixture.cargo_events()))
        self.assert_removed()

    def test_quoted_plus_is_a_routing_reason(self) -> None:
        self.prepare_repository()
        self.fixture.environment["BUILDLOG_OFF"] = "0"
        (Path(self.fixture.environment["BUILD_HOLD_DIR"]) / "other").unlink()
        result = self.run_verify("check", "+", "doc", "--mac", "+")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Mac only (+)", result.stderr)
        rows = cast(list[dict[str, object]], self.fixture.call_records())
        self.assertEqual([row["verb"] for row in rows], ["check", "doc"])
        doc = cast(list[str], self.mac_rows()[-1]["args"])
        self.assertEqual(doc[doc.index("--verb") + 1], "doc")
        self.assert_removed()

    def test_invalid_later_step_leaves_no_temp_folder_or_git_objects(self) -> None:
        self.prepare_repository()
        root = self.fixture.root
        _ = (root / "src/lib.rs").write_text("pub fn unstaged() {}\n")
        objects = set((root / ".git/objects").rglob("*"))
        trace = root / "trace"
        tmpdir = root / "missing-tmp"
        for prepared in ("", str(root)):
            with self.subTest(prepared=prepared):
                result = self.fixture.verify("check", "+", "lint", "--keep-going", extra={
                    "BUILDLOG_PREPARED_COPY": prepared, "TMPDIR": str(tmpdir), "VERIFY_PY_TRACE": str(trace),
                })
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertFalse(tmpdir.exists(), "preflight created TMPDIR")
                self.assertEqual(set((root / ".git/objects").rglob("*")), objects)
                self.assertEqual(list(root.glob("trace*")), [])
                self.assertIn("Usage:", result.stderr)
        self.assertFalse(self.events.exists())
        self.assertEqual(self.fixture.cargo_events(), [])
        self.assertEqual(self.fixture.call_records(), [])
        self.assert_removed()

    def test_invalid_later_arguments_never_start_first_step(self) -> None:
        cases = (
            (("test", "--filter", "bad name"), "Usage:"),
            (("check", "--features"), "Usage:"),
            (("doc", "--features", "--bad"), "Usage:"),
            (("lint", "--keep-going"), "Usage:"),
            (("doc", "--mac", "--features", "extra"), "--mac needs a reason"),
            (("doc", "--target", "missing"), "missing"),
        )
        for later, message in cases:
            with self.subTest(later=later):
                result = self.run_verify("check", "+", *later)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(message, result.stderr)
                self.assertEqual(self.fixture.call_records(), [])
                self.assertEqual(self.mac_rows(), [])
                self.assertFalse(self.events.exists())
                self.assertEqual(self.fixture.cargo_events(), [])
                self.assert_removed()

    def test_each_step_checks_its_package_when_it_runs(self) -> None:
        (Path(self.fixture.environment["BUILD_HOLD_DIR"]) / "other").unlink()
        result = self.run_verify("check", "sample", "--local", "requires Linux", "+", "doc", "missing")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("package missing not found", result.stderr)
        calls = [event["args"][0] for event in self.fixture.cargo_events()]
        self.assertEqual([verb for verb in calls if verb != "metadata"], ["check"])
        self.assertEqual(self.mac_rows(), [])
        self.assert_removed()

    def test_step_list_refuses_unsupported_verbs_and_empty_steps(self) -> None:
        for arguments in (("check", "+", "build"), ("help", "+", "check"),
                          ("test,lint", "+", "check"), ("+", "check"),
                          ("check", "+"), ("check", "+", "+", "test")):
            with self.subTest(arguments=arguments):
                result = self.run_verify(*arguments)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
                self.assertIn("step list", result.stderr)
        self.assertFalse(self.events.exists())
        self.assertEqual(self.fixture.cargo_events(), [])
        self.assert_removed()

    def test_first_failure_ends_the_list_with_its_status(self) -> None:
        self.fixture.environment.update({"TEST_MAC_ALLOWED": "0", "TEST_LOCAL_STATUS": "41"})
        result = self.run_verify("test", "sample", "--filter", "exact", "+", "check", "--keep-going")
        self.assertEqual(result.returncode, 41, result.stdout + result.stderr)
        calls = [event["args"] for event in self.fixture.cargo_events()]
        self.assertEqual(sum(args[:2] == ["nextest", "run"] for args in calls), 1)
        self.assertTrue(all(args[0] in ("metadata", "nextest") for args in calls))
        self.assert_removed()

    def test_step_list_prepares_only_the_first_request(self) -> None:
        request_path = self.fixture.root / "request.json"
        self.fixture.environment.update({
            "BUILDLOG_FIRST_STEP_REQUEST": str(request_path),
            "BUILDLOG_FIRST_STEP_EPISODE": "first-step",
            "BUILDLOG_FIRST_STEP_SINCE": "1",
        })
        result = self.run_verify("check", "sample", "--keep-going", "+", "test", "sample", "--target", "mac")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        request = wire.parse_request(request_path.read_text())
        assert isinstance(request, wire.PlacementRequest)
        self.assertEqual((request.episode, request.identity.step), ("first-step", "check"))
        self.assertIn("--keep-going", request.constraints.command)
        self.assertEqual(self.mac_rows(), [])
        self.assertTrue(all(event["args"][:1] == ["metadata"] for event in self.fixture.cargo_events()))
        self.assert_removed()


if __name__ == "__main__":
    _ = unittest.main()
