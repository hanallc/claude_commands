"""Exercise seed selection, publication, and Cargo's use of a seed."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Generator, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import override
from unittest.mock import patch

from scripts.buildlog import index
from scripts.lib import cargo_seed, conf
from scripts.lib.cargo_output import CargoOutputRoots, CargoOutputUnavailable
from scripts.lib.file_lock import LockResult, exclusive


@unittest.skipUnless(sys.platform == "linux", "the seed uses Linux directory publication")
class CargoSeedTests(unittest.TestCase):
    profile: cargo_seed.HostCompileProfile = cargo_seed.HostCompileProfile("dev")
    root: Path = Path()
    cargo_root: Path = Path()
    registry: Path = Path()
    roots: CargoOutputRoots = CargoOutputRoots(Path(), Path(), Path())
    limits: cargo_seed.SeedLimits = cargo_seed.SeedLimits(Path(), 0, 0)
    staging: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.cargo_root = self.root / "cargo"
        self.cargo_root.mkdir()
        self.staging = self.cargo_root / cargo_seed.STAGING_DIRECTORY
        self.registry = self.root / "cargo-home/registry/src"
        self.registry.mkdir(parents=True)
        workspace = self.root / "workspace"
        workspace.mkdir()
        self.roots = CargoOutputRoots(workspace, workspace / "target", self.cargo_root / workspace.name)
        self.limits = cargo_seed.SeedLimits(self.cargo_root, 50_000, 0)

    def sibling(self, name: str, revision: str = "1234567", completed_at: str = "2026-11-17T17:00:00Z"
                ) -> cargo_seed.Sibling:
        tree = self.root / name
        tree.mkdir()
        sibling = cargo_seed.Sibling(tree, self.cargo_root / name, tree / "target", revision, completed_at)
        for path in (sibling.build_directory / "debug" / ".cargo-build-lock",
                     sibling.target_directory / "debug" / ".cargo-lock",
                     sibling.target_directory / "debug" / ".cargo-artifact-lock"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        self.library(sibling.build_directory / "debug", name)
        return sibling

    def library(self, profile: Path, content: str) -> None:
        registry_input = self.registry / "fixture.rs"
        registry_input.touch()
        digest = "1234567890abcdef"
        fingerprint = profile / ".fingerprint" / f"example-{digest}"
        fingerprint.mkdir(parents=True)
        for file in ("lib-example", "lib-example.json", "dep-lib-example", "invoked.timestamp"):
            (fingerprint / file).touch()
        deps = profile / "deps"
        deps.mkdir()
        _ = (deps / f"example-{digest}.d").write_text(f"example.rlib: {registry_input}\n")
        _ = (deps / f"libexample-{digest}.rlib").write_text(content)

    def git(self, root: Path, *arguments: str) -> str:
        environment = {
            "HOME": str(self.root), "PATH": os.environ.get("PATH", os.defpath),
            "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": "file",
        }
        completed = subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, text=True,
                                   check=True, timeout=10, env=environment)
        return completed.stdout.strip()

    def commit(self, name: str) -> str:
        _ = self.git(self.roots.workspace_root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                     "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", name)
        return self.git(self.roots.workspace_root, "rev-parse", "HEAD")

    def test_git_commit_ignores_the_callers_git_directory(self) -> None:
        other = self.root / "other"
        other.mkdir()
        _ = self.git(other, "init")
        _ = self.git(self.roots.workspace_root, "init")
        with patch.dict(os.environ, {"GIT_DIR": str(other / ".git")}):
            revision = self.commit("fixture")
        self.assertEqual(self.git(self.roots.workspace_root, "rev-parse", "HEAD"), revision)
        self.assertEqual(self.git(other, "rev-list", "--all"), "")

    def test_configured_targets_skip_the_host_profile(self) -> None:
        directory = self.roots.workspace_root
        config = directory / ".cargo/config.toml"
        config.parent.mkdir()
        environment = {"HOME": str(self.root), "CARGO_HOME": str(self.registry.parent.parent)}
        policy_path = self.root / "seed.conf"
        mirror = self.root / "mirror"
        parent = mirror.joinpath(*directory.parent.parts[1:])
        parent.parent.mkdir(parents=True)
        parent.symlink_to(self.cargo_root, target_is_directory=True)
        _ = policy_path.write_text(f"roots.slot_cargo={self.cargo_root}\nseed.enabled=1\n"
                                   + f"roots.cargo_paths.fixture={mirror}\n"
                                   + "seed.max_gib=1\nseed.min_free_gib=0\n")
        policy = conf.read(policy_path)
        self.assertFalse(cargo_seed.uses_configured_target(["check"], str(directory), environment))
        for contents, arguments, extra in (
            ('[build]\ntarget="host-tuple"\n', [], {}),
            ('[build]\ntarget=["x86_64-unknown-linux-gnu"]\n', [], {}),
            ("", ["--config", 'build.target="host-tuple"'], {}),
            ("", [], {"CARGO_BUILD_TARGET": "host-tuple"}),
        ):
            with self.subTest(contents=contents, arguments=arguments, extra=extra):
                _ = config.write_text(contents)
                with (patch.dict(os.environ, {**environment, **extra}, clear=True),
                      patch.object(cargo_seed, "read_seed_roots", return_value=self.roots),
                      patch.object(cargo_seed, "sibling_history", side_effect=AssertionError("no donor lookup"))):
                    result = cargo_seed.seed_cargo(["cargo", "check", *arguments], str(directory), "fixture", policy)
                self.assertIsInstance(result, cargo_seed.UnsupportedCompile)

    def test_metadata_timeout_returns_a_cold_reason(self) -> None:
        with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("cargo metadata", 5)) as run:
            result = cargo_seed.read_seed_roots(["cargo", "+stable"], ["check"], str(self.root), {})
        run.assert_called_once_with(["cargo", "+stable", "metadata", "--format-version", "1", "--no-deps"],
                                    cwd=str(self.root), env={}, capture_output=True, text=True, check=False,
                                    timeout=cargo_seed.METADATA_TIMEOUT_SECONDS)
        self.assertEqual(result, CargoOutputUnavailable("cargo metadata timed out"))

    def test_nearest_safe_sibling_wins(self) -> None:
        _ = self.git(self.roots.workspace_root, "init")
        far_revision = self.commit("far")
        near_revision = self.commit("near")
        _ = self.commit("head")
        far = self.sibling("far", far_revision, "2026-11-17T19:00:00Z")
        first = self.sibling("first", near_revision, "2026-11-17T18:00:00Z")
        near = self.sibling("near", near_revision)
        other = self.sibling("other", near_revision, "2026-11-17T16:00:00Z")
        unknown = self.sibling("unknown", "1234567")
        ranked = cargo_seed.rank_siblings(self.roots.workspace_root, [far, near, first, unknown, other])
        cases: list[tuple[str, set[Path], str]] = [
            ("nearest", set(), "first"),
            ("busy", set(), "near"),
            ("admitted", {first.worktree, near.worktree}, "other"),
            ("no donor", {first.worktree, near.worktree, other.worktree, far.worktree}, ""),
        ]
        for case, admitted, expected in cases:
            with self.subTest(case=case):
                busy_lock = first.target_directory / "debug/.cargo-lock"
                with self.hold_if_busy(busy_lock, case == "busy"):
                    result = cargo_seed.seed(self.roots, self.profile, self.registry, self.limits, ranked,
                                             lambda: admitted)
                if expected:
                    self.assertIsInstance(result, cargo_seed.Seeded)
                    assert isinstance(result, cargo_seed.Seeded)
                    self.assertEqual(result.donor.distance, 1)
                    self.assertEqual(result.donor.sibling.worktree.name, expected)
                    output = next(self.roots.build_directory.glob("debug/deps/*.rlib"))
                    self.assertEqual(output.read_text(), expected)
                    shutil.rmtree(self.roots.build_directory)
                else:
                    self.assertEqual(result, cargo_seed.ColdStart("no safe sibling"))
                    self.assertFalse(self.roots.build_directory.exists())

    @staticmethod
    @contextmanager
    def hold_if_busy(path: Path, busy: bool) -> Generator[None]:
        if busy:
            with exclusive(path) as held:
                assert held is LockResult.ACQUIRED
                yield
        else:
            yield

    def test_history_selects_the_latest_matching_compile_per_folder(self) -> None:
        logs = self.root / "logs"
        logs.mkdir()
        dev = cargo_seed.HostCompileProfile("dev")
        with closing(sqlite3.connect(index.index_path_in(logs))) as connection:
            _ = connection.execute("""CREATE TABLE steps (
                id TEXT, host TEXT, rustc TEXT, status INTEGER, repo_identity TEXT,
                step TEXT, worktree TEXT, build_directory TEXT, target_directory TEXT,
                sha TEXT, ended_at TEXT, build_base TEXT
            )""")
            base: dict[str, str | list[str]] = {"command": "check", "profile": "dev", "target": []}
            rows = [
                ("old", "host", "rustc", 0, "repo", "check", "1111111", base),
                ("latest", "host", "rustc", 0, "repo", "check", "2222222", base),
                ("toolchain", "host", "other", 0, "repo", "check", "3333333", base),
                ("host", "other", "rustc", 0, "repo", "check", "3333333", base),
                ("repo", "host", "rustc", 0, "other", "check", "3333333", base),
                ("failed", "host", "rustc", 1, "repo", "check", "3333333", base),
                ("fmt", "host", "rustc", 0, "repo", "fmt", "3333333", base),
                ("profile", "host", "rustc", 0, "repo", "check", "3333333", {**base, "profile": "release"}),
                ("target", "host", "rustc", 0, "repo", "check", "3333333", {**base, "target": ["other"]}),
            ]
            for order, (key, host, toolchain, status, repo, step, sha, shape) in enumerate(rows):
                _ = connection.execute("INSERT INTO steps VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                       (key, host, toolchain, status, repo, step, "/tree", "/build", "/target",
                                        sha, f"2026-11-17T17:{order:02}:00Z", json.dumps(shape)))
            connection.commit()
        history = cargo_seed.sibling_history(logs, "repo", "host", "rustc", dev)
        self.assertEqual(history, [cargo_seed.Sibling(Path("/tree"), Path("/build"), Path("/target"),
                                                      "2222222", "2026-11-17T17:01:00Z")])

    def test_fixture_policy_and_disk_limits(self) -> None:
        config = self.root / "build_folders.conf"
        _ = config.write_text(f"roots.slot_cargo={self.cargo_root}\nseed.enabled=0\n"
                              + "seed.enabled.fixture=1\nseed.max_gib=1\nseed.min_free_gib=0\n")
        policy = conf.read(config)
        self.assertIsInstance(cargo_seed.seed_limits(policy, "other"), cargo_seed.SeedDisabled)
        self.assertEqual(cargo_seed.seed_limits(policy, "fixture"),
                         cargo_seed.SeedLimits(self.cargo_root, 1 << 30, 0))
        donor = cargo_seed.RankedSibling(self.sibling("limits"), 0)
        for limits, reason in ((replace(self.limits, max_bytes=1), "sibling exceeds the size cap"),
                               (replace(self.limits, min_free_bytes=1 << 60), "copy would cross the free space floor")):
            with self.subTest(reason=reason):
                result = cargo_seed.seed(self.roots, self.profile, self.registry, limits, [donor], set)
                self.assertEqual(result, cargo_seed.ColdStart(reason))
                self.assertFalse(self.roots.build_directory.exists())
                self.assertEqual(list(self.staging.glob("*")), [])

    def test_competing_seed_and_cargo_cannot_overwrite_a_directory(self) -> None:
        donor = cargo_seed.RankedSibling(self.sibling("race"), 0)
        source_copy = shutil.copy2
        for competitor in ("seed", "cargo"):
            with self.subTest(competitor=competitor), ThreadPoolExecutor(max_workers=2) as pool:
                writing, release = Event(), Event()

                def copy(source: Path, destination: Path, *, follow_symlinks: bool = True) -> Path | str:
                    writing.set()
                    if not release.wait(timeout=3):
                        raise TimeoutError("fixture did not release the copy")
                    return source_copy(source, destination, follow_symlinks=follow_symlinks)

                with patch("shutil.copy2", side_effect=copy):
                    second: Future[cargo_seed.Seeded | cargo_seed.ColdStart | cargo_seed.BuildDirectoryExists] | None = None
                    first = pool.submit(cargo_seed.seed, self.roots, self.profile, self.registry, self.limits, [donor], set)
                    try:
                        self.assertTrue(writing.wait(timeout=3))
                        with self.assertRaises(BlockingIOError), cargo_seed.destination_guards(
                            (self.roots.build_directory,), self.limits
                        ):
                            self.fail("seed must hold the destination lock during the copy")
                        with self.assertRaises(BlockingIOError), cargo_seed.checkout_guard(
                            self.roots.workspace_root, cargo_seed.CheckoutAccess.REMOVE
                        ):
                            self.fail("seed must hold the checkout guard during the copy")
                        if competitor == "seed":
                            second = pool.submit(cargo_seed.seed, self.roots, self.profile, self.registry,
                                                 self.limits, [donor], set)
                        else:
                            self.roots.build_directory.mkdir()
                    finally:
                        release.set()
                    result = first.result(timeout=5)
                if second is not None:
                    self.assertIsInstance(result, cargo_seed.Seeded)
                    self.assertIsInstance(second.result(timeout=5), cargo_seed.BuildDirectoryExists)
                else:
                    self.assertIsInstance(result, cargo_seed.BuildDirectoryExists)
                    self.assertEqual(list(self.roots.build_directory.iterdir()), [])
                self.assertEqual(list(self.staging.glob("*")), [])
                shutil.rmtree(self.roots.build_directory)

    def test_interrupted_copy_removes_the_partial_stage(self) -> None:
        donor = cargo_seed.RankedSibling(self.sibling("interrupted"), 0)

        def interrupt(source: Path, destination: Path, *, follow_symlinks: bool = True) -> str:
            _ = shutil.copyfile(source, destination, follow_symlinks=follow_symlinks)
            raise KeyboardInterrupt

        with patch("shutil.copy2", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            _ = cargo_seed.seed(self.roots, self.profile, self.registry, self.limits, [donor], set)
        self.assertFalse(self.roots.build_directory.exists())
        self.assertEqual(list(self.staging.glob("*")), [])
        retry = cargo_seed.seed(self.roots, self.profile, self.registry, self.limits, [donor], set)
        self.assertIsInstance(retry, cargo_seed.Seeded)

    def test_abandoned_stage_is_removed_even_if_cargo_created_the_destination(self) -> None:
        stage = self.staging / "published"
        self.staging.mkdir(mode=0o700)
        stage.mkdir()
        self.roots.build_directory.mkdir()
        ready = self.roots.build_directory / "cargo-owned"
        ready.touch()
        result = cargo_seed.seed(self.roots, self.profile, self.registry, self.limits, (), set)
        self.assertIsInstance(result, cargo_seed.BuildDirectoryExists)
        self.assertFalse(stage.exists())
        self.assertTrue(ready.is_file())

    def test_warm_recovery_skips_a_busy_disk_lock(self) -> None:
        stage = self.staging / "active"
        self.staging.mkdir(mode=0o700)
        stage.mkdir()
        self.roots.build_directory.mkdir()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with exclusive(self.cargo_root / ".cargo-seed.lock"):
                result = pool.submit(cargo_seed.seed, self.roots, self.profile, self.registry, self.limits, (), set)
                self.assertIsInstance(result.result(timeout=2), cargo_seed.BuildDirectoryExists)
                self.assertTrue(stage.is_dir())
        self.assertIsInstance(cargo_seed.seed(self.roots, self.profile, self.registry, self.limits, (), set),
                              cargo_seed.BuildDirectoryExists)
        self.assertFalse(stage.exists())

    def test_seed_recovers_interrupted_stages(self) -> None:
        self.staging.mkdir(mode=0o700)
        (self.staging / "empty").mkdir()
        partial = self.staging / "partial" / "output"
        partial.mkdir(parents=True)
        _ = (partial / "compile").write_text("interrupted copy")
        (self.staging / "link").symlink_to(self.roots.workspace_root, target_is_directory=True)
        donor = cargo_seed.RankedSibling(self.sibling("owned"), 0)

        result = cargo_seed.seed(self.roots, self.profile, self.registry, self.limits, [donor], set)

        self.assertIsInstance(result, cargo_seed.Seeded)
        self.assertTrue(self.roots.build_directory.is_dir())
        self.assertTrue(self.roots.workspace_root.is_dir())
        self.assertEqual(list(self.staging.iterdir()), [])

    def test_seed_refuses_a_symlink_as_its_private_directory(self) -> None:
        foreign = self.root / "foreign"
        foreign.mkdir()
        self.staging.symlink_to(foreign, target_is_directory=True)
        compiled = foreign / "cargo-output"
        _ = compiled.write_text("foreign")
        donor = cargo_seed.RankedSibling(self.sibling("owned"), 0)
        with self.assertRaisesRegex(OSError, "is not a private seed directory"):
            _ = cargo_seed.seed(self.roots, self.profile, self.registry, self.limits, [donor], set)
        self.assertEqual(compiled.read_text(), "foreign")
        self.assertFalse(self.roots.build_directory.exists())

    def test_destination_locks_have_a_fixed_limit(self) -> None:
        count = cargo_seed.DESTINATION_LOCK_COUNT
        for number in range(count * 3):
            with cargo_seed.destination_guards((self.cargo_root / f"build-{number}",), self.limits):
                pass
        locks = list((self.cargo_root / ".cargo-seed-locks").iterdir())
        self.assertGreater(len(locks), 1)
        self.assertLessEqual(len(locks), count)

    def fixture_cargo(self) -> tuple[str, str]:
        rustup = shutil.which("rustup")
        if rustup:
            tools = [subprocess.run([rustup, "which", name], capture_output=True, text=True, timeout=10, check=False)
                     for name in ("cargo", "rustc")]
            if all(tool.returncode == 0 for tool in tools):
                return tools[0].stdout.strip(), tools[1].stdout.strip()
        cargo, rustc = shutil.which("cargo"), shutil.which("rustc")
        if cargo and rustc:
            return cargo, rustc
        self.skipTest("Cargo and rustc are required")

    @staticmethod
    def write_crate(root: Path, value: str) -> None:
        (root / "src").mkdir(parents=True)
        (root / "local/src").mkdir(parents=True)
        _ = (root / "Cargo.toml").write_text(
            '[package]\nname="fixture"\nversion="0.1.0"\nedition="2021"\n'
            + '[dependencies]\nlocal={path="local"}\nportable="1"\n')
        _ = (root / "local/Cargo.toml").write_text(
            '[package]\nname="local"\nversion="0.1.0"\nedition="2021"\n')
        _ = (root / "local/src/lib.rs").write_text(f'pub fn value() -> &\'static str {{ "{value}" }}\n')
        _ = (root / "build.rs").write_text(
            'fn main() { let dir = std::env::var("OUT_DIR").unwrap(); '
            + 'std::fs::write(std::path::Path::new(&dir).join("generated"), '
            + f'"{value}").unwrap(); }}\n')
        _ = (root / "src/main.rs").write_text(
            'fn main() { println!("{} {} {}", local::value(), portable::VALUE, '
            + 'include_str!(concat!(env!("OUT_DIR"), "/generated"))); }\n')

    def build_crate(self, cargo: str, roots: CargoOutputRoots, environment: Mapping[str, str]
                    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run([cargo, "build", "--offline", "-v"], cwd=roots.workspace_root,
                                env={**environment, "CARGO_BUILD_BUILD_DIR": str(roots.build_directory),
                                     "CARGO_TARGET_DIR": str(roots.target_directory)},
                                capture_output=True, text=True, check=False, timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_real_cargo_reuses_a_registry_unit_and_rebuilds_local_inputs(self) -> None:
        cargo, rustc = self.fixture_cargo()
        package = self.registry / "fixture/portable-1.0.0"
        (package / "src").mkdir(parents=True)
        _ = (package / "Cargo.toml").write_text(
            '[package]\nname="portable"\nversion="1.0.0"\nedition="2021"\n')
        _ = (package / "src/lib.rs").write_text('pub const VALUE: &str = "registry";\n')
        _ = (package / ".cargo-checksum.json").write_text('{"package":null,"files":{}}\n')
        home = self.registry.parent.parent
        _ = (home / "config.toml").write_text(
            '[source.crates-io]\nreplace-with="fixture"\n[source.fixture]\n'
            + f'directory={json.dumps(str(package.parent))}\n')
        env = {"HOME": str(self.root), "PATH": os.environ.get("PATH", os.defpath), "CARGO_HOME": str(home),
               "RUSTC": rustc, "LANG": "C", "CARGO_TERM_COLOR": "never"}
        donor_workspace = self.root / "donor"
        donor = CargoOutputRoots(donor_workspace, donor_workspace / "target", self.cargo_root / "donor")
        self.write_crate(donor.workspace_root, "old")
        self.write_crate(self.roots.workspace_root, "new")
        before = self.build_crate(cargo, donor, env)
        self.assertIn("Compiling portable", before.stderr)
        self.assertTrue(list(donor.build_directory.glob("debug/build/*/out/generated")))
        sibling = cargo_seed.Sibling(donor_workspace, donor.build_directory, donor.target_directory, "1234567", "")
        self.limits = replace(self.limits, max_bytes=16 << 20)
        copied = cargo_seed.seed(self.roots, self.profile, self.registry, self.limits,
                                  [cargo_seed.RankedSibling(sibling, 0)], set)
        self.assertIsInstance(copied, cargo_seed.Seeded)
        self.assertFalse(list(self.roots.build_directory.glob("debug/build/*/out")))
        self.assertFalse(list(self.roots.build_directory.glob("debug/deps/liblocal*")))
        self.assertTrue(list(self.roots.build_directory.glob("debug/deps/libportable*")))
        after = self.build_crate(cargo, self.roots, env)
        self.assertIn("Fresh portable", after.stderr)
        self.assertIn("Compiling local", after.stderr)
        program = self.roots.target_directory / "debug/fixture"
        ran = subprocess.run([str(program)], capture_output=True, text=True, check=True, timeout=5)
        self.assertEqual(ran.stdout, "new registry new\n")


if __name__ == "__main__":
    _ = unittest.main()
