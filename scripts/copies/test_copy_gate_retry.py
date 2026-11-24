"""Test gate retries with a fixture queue and a local command."""

from collections.abc import Generator
from contextlib import ExitStack, contextmanager, nullcontext, redirect_stderr, redirect_stdout
import os
from pathlib import Path
import sys
import tempfile
from textwrap import dedent
from typing import final, override
import unittest
from unittest import mock

from scripts.build_hold.owning_session import NoOwningSession
from scripts.build_queue import admission_client as client, admission_protocol as wire
from scripts.build_queue import admission_records as records, execution_identity, first_step, placement, queue_place
from scripts.copies import copies
from scripts.lib import build_folders
from scripts.lib.build_steps import MAC_LINUX_PRELAUNCH_REFUSED_STATUS
from scripts.mac_test import claim_state


@contextmanager
def redirect_descriptor(descriptor: int, output: int) -> Generator[None]:
    with ExitStack() as restore:
        original = os.dup(descriptor)
        _ = restore.callback(os.close, original)
        _ = restore.callback(os.dup2, original, descriptor, inheritable=os.get_inheritable(descriptor))
        _ = os.dup2(output, descriptor)
        yield


@final
class CopyGateRetryTests(unittest.TestCase):
    root: Path = Path()
    tree: Path = Path()
    runs: Path = Path()
    lease_count: int = 0

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.tree = self.root / "copy"
        self.tree.mkdir()
        self.runs = self.root / "runs"
        self.enterContext(mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": ""}))
        _ = self.enterContext(mock.patch.object(copies, "caller_owner", return_value="fixture"))
        _ = self.enterContext(mock.patch.object(copies, "requested_source", return_value=nullcontext(self.root)))
        _ = self.enterContext(mock.patch.object(copies, "commit_at", return_value="fixture-commit"))
        _ = self.enterContext(mock.patch.object(build_folders, "tool_folder", return_value=nullcontext(self.root)))
        _ = self.enterContext(mock.patch.object(records, "process_start_time", return_value="123"))
        _ = self.enterContext(mock.patch("scripts.build_queue.admission_client._call",
                                         side_effect=AssertionError("fixture must use the mocked queue")))
        _ = self.enterContext(mock.patch.object(copies, "standing", self.leased_copy))

    @contextmanager
    def leased_copy(self, *_arguments: object, **_options: object) -> Generator[Path]:
        self.lease_count += 1
        with (self.root / "lease").open("wb") as lease:
            token = copies.COMMAND_LOCKS.set((lease.fileno(),))
            try:
                yield self.tree
            finally:
                copies.COMMAND_LOCKS.reset(token)

    def run_gate(self, *, report_failure: bool) -> tuple[int, bytes, bytes]:
        script = self.root / "gate.py"
        _ = script.write_text(dedent("""\
            import os
            from pathlib import Path
            import sys

            sys.path.insert(0, sys.argv[1])
            from scripts.build_queue import admission_wire as wire
            from scripts.build_queue.placement import MAC_LINUX_NO_ANSWER
            from scripts.lib.build_steps import CLAIM_FAILURE_SUFFIX, MAC_LINUX_PRELAUNCH_REFUSED_STATUS

            assert Path.cwd() == Path(sys.argv[2])
            assert Path(os.environ["BUILDLOG_PREPARED_COPY"]) == Path.cwd()
            with os.fdopen(int(os.environ["BUILDLOG_PREPARED_GATE_FD"]), "rb") as handoff:
                assert handoff.read(1) == b"1"
            assert os.environ["COPIES_STEP_OWNER"] == "fixture"
            os.fstat(int(os.environ["COPIES_COMMAND_LEASE_FDS"]))
            path = Path(os.environ["BUILDLOG_PLACEMENT_FILE"])
            assignment = wire.read_assignment(path).assignment
            assert os.environ["BUILDLOG_PLACEMENT_EPISODE"] == assignment.episode
            with Path(sys.argv[3]).open("a") as runs:
                runs.write(assignment.attempt + "\\n")
            attempt = assignment.attempt.encode("ascii")
            os.write(1, attempt + b" stdout\\xff\\n")
            os.write(2, attempt + b" stderr\\xfe\\n")
            if assignment.attempt == "guest-attempt":
                if sys.argv[4] == "report":
                    refusal = wire.ClaimFailure("refusal", assignment, MAC_LINUX_NO_ANSWER)
                    path.with_name(path.name + CLAIM_FAILURE_SUFFIX).write_text(wire.encode_request(refusal))
                raise SystemExit(MAC_LINUX_PRELAUNCH_REFUSED_STATUS)
            raise SystemExit(7)
            """))
        repository = Path(__file__).resolve().parents[2]
        command = (sys.executable, str(script), str(repository), "{tree}", str(self.runs),
                   "report" if report_failure else "no-report")

        remote = placement.PlacementDestination("ssh", "mac-linux", "fixture-guest", claim_state.WHOLE_MAC, 1)
        local = placement.PlacementDestination("local", "natedev", "fixture-local", claim_state.WHOLE_MAC)
        owner = placement.SubmittingOwner("fixture-local", os.getpid(), "123")
        source_key = execution_identity.SourceKeyKnown("fixture-source")
        session = NoOwningSession()
        guest = placement.PlacementAssignment("fixture-episode", "guest-attempt", remote, source_key,
                                              owner, session, 1.0)
        replacement = placement.PlacementAssignment(guest.episode, "local-attempt", local, source_key,
                                                     owner, session, guest.since)
        profile = records.ProgramProfile("gate", str(self.root), str(self.root),
                                         records.HistoryPeakSizing(1, records.FirstProgramPeak(1), 0.0))
        request = wire.PlacementRequest(guest.episode, owner, guest.since, profile,
            placement.PlacementConstraints(source_key, command, str(self.root), "", session,
                (placement.PlacementCandidate(remote), placement.PlacementCandidate(local))))
        settled = wire.DispatchStatus(guest, placement.ClosedAssignment(placement.MAC_LINUX_NO_ANSWER),
                                      placement.PermissionConsumed(1.5), retry=placement.RetryAt(2.0))
        second_grant = wire.LocalPlacementGranted(replacement, wire.Admitted("local", 1, "gate", "", "", {}))

        stdout_path = self.root / "stdout"
        stderr_path = self.root / "stderr"
        with (mock.patch("scripts.build_queue.first_step._gate_request", return_value=request),
              mock.patch.object(queue_place, "wait_for_placement", side_effect=(wire.Assignment(guest), second_grant))
              as place,
              mock.patch.object(client, "claim_failure", return_value=settled) as settle,
              mock.patch.object(client, "cancel_placement", return_value=wire.Ok()) as cancel,
              stdout_path.open("w", encoding="utf-8") as output,
              stderr_path.open("w", encoding="utf-8") as errors,
              redirect_descriptor(1, output.fileno()), redirect_descriptor(2, errors.fileno()),
              redirect_stdout(output), redirect_stderr(errors)):
            status = copies.main(["run", "--repo", str(self.root), "--commit", "HEAD",
                                  "--targets", "natedev,mac-linux", "--", *command])

        if report_failure:
            settle.assert_called_once_with(wire.ClaimFailure("refusal", guest, placement.MAC_LINUX_NO_ANSWER))
            self.assertEqual(place.call_args_list, [mock.call(request, "the caller left before first-step admission")] * 2)
        else:
            settle.assert_not_called()
            place.assert_called_once()
        cancel.assert_called_once()
        self.assertEqual(self.lease_count, 1)
        return status, stdout_path.read_bytes(), stderr_path.read_bytes()

    def test_gate_runs_again_on_the_replacement_assignment(self) -> None:
        status, output, errors = self.run_gate(report_failure=True)

        self.assertEqual(status, 7, errors)
        self.assertEqual(self.runs.read_text().splitlines(), ["guest-attempt", "local-attempt"])
        retry = "placement: retry — fixture-guest (mac-linux) did not answer. Requesting placement again.\n"
        self.assertEqual(output, b"guest-attempt stdout\xff\n" + retry.encode("utf-8") + b"local-attempt stdout\xff\n")
        self.assertEqual(errors, b"guest-attempt stderr\xfe\nlocal-attempt stderr\xfe\n")

    def test_gate_without_a_claim_failure_stops_after_one_run(self) -> None:
        status, output, errors = self.run_gate(report_failure=False)

        self.assertEqual(status, MAC_LINUX_PRELAUNCH_REFUSED_STATUS)
        self.assertEqual(self.runs.read_text().splitlines(), ["guest-attempt"])
        self.assertEqual(output, b"guest-attempt stdout\xff\n")
        self.assertTrue(errors.startswith(b"guest-attempt stderr\xfe\nGate placement blocked. "), errors)

    def test_non_gate_runs_once_without_admission(self) -> None:
        script = self.root / "ordinary.py"
        _ = script.write_text(dedent("""\
            from pathlib import Path
            import sys

            with Path(sys.argv[1]).open("a") as runs:
                runs.write("ordinary\\n")
            raise SystemExit(79)
            """))
        with mock.patch.object(first_step, "admitted_run", wraps=first_step.admitted_run) as admit:
            status = copies.main(["run", "--repo", str(self.root), "--commit", "HEAD",
                                  "--", sys.executable, str(script), str(self.runs)])

        self.assertEqual(status, MAC_LINUX_PRELAUNCH_REFUSED_STATUS)
        self.assertEqual(self.runs.read_text().splitlines(), ["ordinary"])
        admit.assert_not_called()
        self.assertEqual(self.lease_count, 1)
