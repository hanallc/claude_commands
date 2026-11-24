"""Exercise the Verify step list with private routing and Cargo fixtures."""
from __future__ import annotations

import json
from pathlib import Path
from typing import cast
import unittest
from unittest import mock

from scripts.build_queue import admission_protocol as wire, admission_service as service
from scripts.build_queue.admission_test_support import caller_admission
from scripts.build_targets import target_config
from scripts.direct.verify_step_routes_test_support import VerifyStepRoutesFixture


class VerifyStepListTests(VerifyStepRoutesFixture):
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
        admission = caller_admission(self.fixture, root, self.fixture.environment)
        previous = admission.server.handle_request
        requests: dict[str, wire.PlacementRequest] = {}

        def handle(request: wire.Request, connection: service.Connection | service.NoConnection = service.NO_CONNECTION
                   ) -> list[wire.Reply]:
            if isinstance(request, wire.PlacementRequest):
                _ = requests.setdefault(request.episode, request)
            return previous(request, connection)

        with mock.patch.object(admission.server, "handle_request", side_effect=handle):
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
        self.assertTrue(all(request.run == run for request in placements))
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
            (("doc", "missing"), "package missing not found"),
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
                self.assertTrue(all(event["args"][:1] == ["metadata"] for event in self.fixture.cargo_events()))
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
