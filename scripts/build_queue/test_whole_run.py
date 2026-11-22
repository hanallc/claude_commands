"""Check whole-run ownership with private claim files."""
from collections.abc import Callable
from contextlib import nullcontext, redirect_stdout
from io import StringIO
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from scripts.build_hold.owning_session import NoOwningSession
from scripts.build_queue import admission_wire as wire, queue_place, whole_run
from scripts.build_queue.admission_jobs import StepJob
from scripts.mac_test import claim_state, mac_test
from scripts.lint.memory_test_support import gate_isolation_environment


def write_guest_registry(root: Path) -> None:
    _ = (root / "build_targets.conf").write_text(
        "targets=mac\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n"
        + "pinned_targets=mac-linux\nkind.mac-linux=ssh\nplatform.mac-linux=linux\n"
        + "settings.mac-linux=mac_linux.conf\nhost.mac-linux=mac\n")


class WholeRunTests(unittest.TestCase):
    def test_host_claim_and_reservation_release_in_order(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_guest_registry(root)
            paths = claim_state.StatePaths(root / "mac")
            calls: list[str] = []
            job = StepJob("gate", "queue", modules=12)
            owner = os.getpid()

            def reserve(request: queue_place.QueueRequest, _output: Callable[[wire.Wait], None]) -> wire.Admitted:
                self.assertFalse(paths.run.exists())
                assert isinstance(request, queue_place.RefreshedRequest)
                admission = request.refresh()
                self.assertEqual(admission.place, wire.WholeRunPlace("mac-linux"))
                self.assertEqual(admission.profile.peak.bytes, 0)
                self.assertEqual(admission.identity.job, job)
                calls.append("reserve")
                return wire.Admitted("guest-grant", 0, "", "", "", {})

            def finish_existing_job(_seconds: float) -> None:
                self.assertFalse(paths.run.exists())
                self.assertEqual([claim.scope for claim in claim_state.read_claims(paths)], [existing_scope])
                calls.append("wait for Mac")
                mac_test.claim_path(paths, existing_scope).unlink()

            def release(reservation: str, status: int, _timing: wire.CleanupTiming) -> wire.Ok:
                self.assertFalse(paths.run.exists())
                self.assertEqual((reservation, status), ("guest-grant", 7))
                calls.append("release reservation")
                return wire.Ok()

            def release_claim(paths: claim_state.StatePaths, pid: int, identity: claim_state.ClaimIdentity,
                              settings: str) -> mac_test.ClaimsReleased | mac_test.ClaimReleaseRefused:
                calls.append("release claim")
                return real_release(paths, pid, identity, settings)

            real_release = mac_test.release_exact_claims
            existing_scope = claim_state.RepositoryScope("existing")
            existing = mac_test.CliArguments()
            existing.pid, existing.what = owner, "existing job"
            with claim_state.locked(paths.lock), redirect_stdout(StringIO()):
                result = mac_test.claim_once(existing, paths, existing_scope, session=NoOwningSession())
            self.assertEqual(result, mac_test.ClaimFinished(0))

            with (patch.dict(os.environ, gate_isolation_environment(root)),
                  patch.object(queue_place, "queue_place_folder", return_value=nullcontext(root)),
                  patch.object(queue_place, "request_place", side_effect=reserve),
                  patch.object(queue_place, "release_place", side_effect=release),
                  patch.object(claim_state, "state_paths", return_value=paths) as state_paths,
                  patch.object(claim_state, "submitting_session", return_value=NoOwningSession()),
                  patch.object(time, "sleep", side_effect=finish_existing_job),
                  patch.object(mac_test, "release_exact_claims", side_effect=release_claim)):
                with whole_run.held(root, "mac-linux", job, job) as run:
                    calls.append("run")
                    self.assertTrue(run.sidecar.is_file())
                    claim, = claim_state.read_claims(paths)
                    self.assertEqual(claim.scope, claim_state.WHOLE_MAC)
                    self.assertEqual((claim.record["pid"], claim.record["what"]), (owner, "mac-linux gate"))
                    args = mac_test.CliArguments()
                    with claim_state.locked(paths.lock), redirect_stdout(StringIO()):
                        result = mac_test.claim_once(args, paths, claim_state.RepositoryScope("other"),
                                                    session=NoOwningSession())
                    self.assertEqual(result, mac_test.ClaimBusy(claim.record))
                    run.status = 7
            state_paths.assert_called_with("mac")
            self.assertEqual(calls, ["reserve", "wait for Mac", "run", "release claim", "release reservation"])

    def test_a_claim_refusal_releases_the_reservation_and_cleanup_errors_raise(self) -> None:
        job = StepJob("gate")
        grant = wire.Admitted("guest-grant", 0, "", "", "", {})
        for case in ("claim refused", "claim cleanup refused", "reservation cleanup refused"):
            with self.subTest(case=case), TemporaryDirectory() as directory:
                root = Path(directory)
                write_guest_registry(root)
                paths = claim_state.StatePaths(root / "mac")
                refusal = mac_test.ClaimReleaseRefused("Mac claim release failed")
                claim_release = refusal if case == "claim cleanup refused" else mac_test.ClaimsReleased(1, ())
                reply = wire.Refused("reservation release failed") if case == "reservation cleanup refused" else wire.Ok()
                with (patch.dict(os.environ, {"CONF_DIR": directory}),
                      patch.object(queue_place, "queue_place_folder", return_value=nullcontext(root)),
                      patch.object(queue_place, "request_place", return_value=grant),
                      patch.object(queue_place, "release_place", return_value=reply) as release,
                      patch.object(claim_state, "state_paths", return_value=paths),
                      patch.object(claim_state, "submitting_session", return_value=NoOwningSession()),
                      patch.object(mac_test, "release_exact_claims", return_value=claim_release) as release_mac):
                    if case == "claim refused":
                        with (patch.object(mac_test, "claim_once", return_value=mac_test.ClaimFinished(10)),
                              self.assertRaisesRegex(whole_run.WholeRunRefused, "mac refused the whole-run claim"),
                              whole_run.held(root, "mac-linux", job, job)):
                            self.fail("a refused claim must end the run")
                        release_mac.assert_not_called()
                    else:
                        message = refusal.reason if case == "claim cleanup refused" else "reservation release failed"
                        with self.assertRaisesRegex(RuntimeError, message), whole_run.held(root, "mac-linux", job, job):
                            self.assertTrue(paths.run.exists())
                        release_mac.assert_called_once()
                    release.assert_called_once_with(grant.reservation, 1, grant.cleanup_timing)
