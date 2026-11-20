"""Real verify.sh token build behavior."""
from __future__ import annotations

import json
from pathlib import Path
import unittest

from scripts.build_queue.admission_test_support import caller_admission
from scripts.direct.verify_token_wait_test_support import VerifyTokenWaitFixture


def harness_artifact(path: str | None, *, package: str = "sample", name: str = "sample-app",
                     kind: str = "bin", test: bool = True) -> dict[str, object]:
    return {"reason": "compiler-artifact", "package_id": f"path+file:///slot#{package}@0.1.0",
            "target": {"name": name, "kind": [kind]}, "profile": {"test": test}, "executable": path, "fresh": True}


class VerifyTokenBuildTests(VerifyTokenWaitFixture):
    def harness_file(self, name: str, *, executable: bool = True) -> Path:
        path = self.root / "target" / name
        path.touch()
        path.chmod(0o755 if executable else 0o644)
        return path

    def harness_environment(self, *artifacts: dict[str, object]) -> dict[str, str]:
        environment = self.build_stub([{"name": "sample-app", "kind": ["bin"]},
                                       {"name": "sample_library", "kind": ["rlib"]}])
        return {**environment, "TEST_CARGO_ARTIFACTS": "\n".join(map(json.dumps, artifacts))}

    def test_build_reports_the_release_harness_under_local_admission_and_the_token(self) -> None:
        _ = self.harness_file("sample-app-stale")
        bin_path = str(self.harness_file("sample-app with spaces"))
        lib_path = str(self.harness_file("sample_library-abcdef"))
        environment = self.harness_environment(
            harness_artifact(bin_path),
            harness_artifact(lib_path, name="sample_library", kind="rlib"),
            harness_artifact(bin_path),
            harness_artifact(lib_path, package="other"),
            harness_artifact(lib_path, name="other"),
            harness_artifact(lib_path, kind="example"),
            harness_artifact(lib_path, test=False),
            harness_artifact(None),
        )
        environment["TEST_CARGO_DIAGNOSTICS"] = json.dumps(harness_artifact(lib_path))
        admission = caller_admission(self, self.root, self.environment)
        rows = ((("--bin", "sample-app", "--locked"), bin_path), (("--lib",), lib_path))
        for selector, path in rows:
            with self.subTest(selector=selector):
                (self.root / "cargo-calls").unlink(missing_ok=True)
                result = self.build(environment, "--test-harness", "--release", "--attached", *selector)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stdout, f"binary: {path}\n")
                self.assertIn(environment["TEST_CARGO_DIAGNOSTICS"], result.stderr)
                words = ("test", "--no-run", "--release", "-p", "sample", *selector,
                         "--message-format=json-render-diagnostics")
                self.assertEqual((self.root / "cargo-calls").read_text().split(), list(words))
                self.assertFalse((self.root / "board" / "locks" / "cargo.d").exists())
        with admission.loop_turn():
            assignments = tuple(admission.server.assignments.values())
        self.assertEqual(len(assignments), 2)
        self.assertTrue(all(saved.assignment.destination.target == "natedev" for saved in assignments))

    def test_build_requires_one_runnable_harness_from_successful_cargo(self) -> None:
        first = str(self.harness_file("first"))
        second = str(self.harness_file("second"))
        non_executable = str(self.harness_file("non-executable", executable=False))
        for case, paths, cargo_status, expected_status in (
            ("missing", (), 0, 1),
            ("ambiguous", (first, second), 0, 1),
            ("non-executable", (non_executable,), 0, 1),
            ("relative", ("target/first",), 0, 1),
            ("failed", (first,), 101, 101),
        ):
            with self.subTest(case=case):
                environment = self.harness_environment(*(harness_artifact(path) for path in paths))
                environment["TEST_CARGO_STATUS"] = str(cargo_status)
                diagnostic = "binary: this line came from Cargo stderr"
                environment["TEST_CARGO_DIAGNOSTICS"] = diagnostic
                result = self.build(environment, "--test-harness", "--release", "--bin", "sample-app")
                self.assertEqual(result.returncode, expected_status, result.stdout + result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertIn(diagnostic, result.stderr)
                if cargo_status == 0:
                    self.assertIn("expected one runnable test harness", result.stderr)

    def test_build_refuses_invalid_harness_requests_before_placement(self) -> None:
        environment = self.harness_environment()
        admission = caller_admission(self, self.root, self.environment)
        for package, options in (
            ("", ("--release", "--lib")),
            ("sample", ("--release",)),
            ("sample", ("--lib",)),
            ("sample", ("--release", "--lib", "--bin", "sample-app")),
            ("sample", ("--release", "--lib", "--mac", "benchmark")),
            ("sample", ("--release", "--lib", "--target", "fixture-ssh")),
        ):
            with self.subTest(package=package, options=options):
                result = self.build(environment, "--test-harness", *options, package=package)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        with admission.loop_turn():
            self.assertFalse(admission.server.assignments)
        self.assertFalse((self.root / "cargo-calls").exists())
        self.assertFalse((self.root / "metadata-calls").exists())

    def test_build_makes_the_packages_binaries_under_the_token_and_prints_their_paths(self) -> None:
        # A smoke or live-shot helper ran raw `cargo build` before it launched
        # the app, and the user approved it by hand (2026-10-23).
        environment = self.build_stub([{"name": "sample", "kind": ["lib"]},
                                       {"name": "sample-app", "kind": ["bin"]},
                                       {"name": "demo", "kind": ["example"]}])
        result = self.build(environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"binary: {self.root / 'target' / 'debug' / 'sample-app'}\n", result.stdout)
        self.assertNotIn("demo", result.stdout)
        self.assertEqual((self.root / "cargo-calls").read_text(), "build --workspace --bin sample-app\n")
        self.assertIn("claim: token cargo acquired", (self.root / "board" / "board.log").read_text())
        self.assertFalse((self.root / "board" / "locks" / "cargo.d").exists())

    def test_build_of_an_example_turns_on_its_required_features(self) -> None:
        environment = self.build_stub([{"name": "sample-app", "kind": ["bin"]},
                                       {"name": "demo", "kind": ["example"], "required-features": ["extra"]}])
        result = self.build(environment, "--example", "demo", "--release", "--features", "fast")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root / "cargo-calls").read_text(),
                         "build --workspace --example demo --release --features sample/fast,sample/extra\n")
        self.assertIn(f"binary: {self.root / 'target' / 'release' / 'examples' / 'demo'}\n", result.stdout)

    def test_build_refuses_a_package_with_no_binary_and_a_target_it_lacks(self) -> None:
        environment = self.build_stub([{"name": "sample", "kind": ["lib"]}])
        result = self.build(environment)
        self.assertEqual(result.returncode, 2)
        self.assertIn("package sample has no binary; name an example with --example <name>", result.stderr)
        result = self.build(environment, "--bin", "other")
        self.assertEqual(result.returncode, 2)
        self.assertIn("package sample has no bin other", result.stderr)
        result = self.build(environment, "--bin", "a", "--example", "b")
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.root / "cargo-calls").exists())



if __name__ == "__main__":
    _ = unittest.main()
