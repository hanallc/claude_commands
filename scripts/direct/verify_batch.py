"""Run the verification steps under the queue identity of verify.sh."""

from __future__ import annotations

if __name__ == "__main__":
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import final

from scripts.buildlog import treekey
from scripts.direct import recorder_state
from scripts.direct.cleanup_lease import CommandLifetime, CleanupLeaseUse, ForegroundCleanupLease, NoCleanupLease
from scripts.lib.build_folders import ForegroundMarker


class LintScope(Enum):
    WORKSPACE = "workspace"
    PACKAGES = "packages"


class NoBaselineProof(Enum):
    SKIP = "skip"


@dataclass(frozen=True)
class RegressionCase:
    package: str
    test: str


@dataclass(frozen=True)
class BaselineProof:
    revision: str
    patch: Path
    cases: tuple[RegressionCase, ...]


@dataclass(frozen=True)
class BatchStepResult:
    status: int
    cached: bool

    def show(self, step: str, scope: str, detail: str = "") -> bool:
        passed = self.status == 0 and not detail
        verdict = "green" if passed else "red"
        cache = "yes" if self.cached else "no"
        reason = f" ({detail})" if detail else ""
        print(f"verify.sh batch {step} {scope} {verdict} cached={cache}{reason}", flush=True)
        return passed


@final
class BatchCommands:
    """Run one command at a time in the caller's process group."""

    def __init__(self, verify: Path, route: Sequence[str],
                 *, cleanup: ForegroundCleanupLease | NoCleanupLease = NoCleanupLease.NONE) -> None:
        self.verify = verify
        self.route = route
        self.cleanup = cleanup
        self.environment = {**os.environ, "UNIT_DETACHED_RUN": "1"}
        _ = self.environment.pop("UNIT_DETACHED_RECORD", None)

    def invoke(self, command: Sequence[str], changes: dict[str, str], *,
               use: CleanupLeaseUse = CleanupLeaseUse.KEEP_IN_SUPERVISOR,
               lifetime: CommandLifetime = CommandLifetime.FOREGROUND,
               stdout: int | None = None) -> subprocess.CompletedProcess[bytes]:
        environment = {**self.environment, **changes}
        if isinstance(self.cleanup, ForegroundCleanupLease):
            return self.cleanup.run(command, cwd=Path.cwd(), environment=environment,
                                    stdin=subprocess.DEVNULL, stdout=stdout, use=use, lifetime=lifetime)
        return subprocess.run(command, env=environment, stdin=subprocess.DEVNULL, stdout=stdout, check=False)

    def run(self, command: Sequence[str], changes: dict[str, str], *,
            use: CleanupLeaseUse = CleanupLeaseUse.KEEP_IN_SUPERVISOR,
            lifetime: CommandLifetime = CommandLifetime.FOREGROUND) -> int:
        result = self.invoke(command, changes, use=use, lifetime=lifetime)
        return result.returncode if result.returncode >= 0 else 128 - result.returncode

    def tree_key(self, repo: Path) -> str:
        result = self.invoke([sys.executable, str(Path(treekey.__file__).resolve()), str(repo),
                              "--exclude-session-dirs"], {},
                             lifetime=CommandLifetime.GROUP, stdout=subprocess.PIPE)
        return result.stdout.decode("ascii").strip() if result.returncode == 0 else ""

    def verify_step(self, step: str, args: Sequence[str], cache_hit: Path,
                    *, format_packages: str = "") -> BatchStepResult:
        cache_hit.unlink(missing_ok=True)
        command = ["bash", str(self.verify), *args, *self.route]
        changes = {
            "VERIFY_BATCH_STEP": step, "VERIFY_BATCH_CACHE_HIT": str(cache_hit),
            "VERIFY_BATCH_LINT_PACKAGES": format_packages,
        }
        if isinstance(self.cleanup, ForegroundCleanupLease):
            changes["VERIFY_BATCH_PLACEMENT_PENDING_FILE"] = str(cache_hit.with_suffix(".placement-pending"))
        status = self.run(command, changes, lifetime=CommandLifetime.VERIFY_STEP)
        return BatchStepResult(status, cache_hit.is_file())


def phase_directory() -> Path:
    """Select the open phase, or use the caller's explicit phase directory."""
    if explicit := os.environ.get("VERIFY_BATCH_PHASE_DIR"):
        return Path(explicit).resolve()
    session = os.environ.get("UNIT_RUN_DIR") or os.environ.get("UNIT_SESSION_DIR")
    if session:
        phases = recorder_state.read_open_phase_ids(Path(session))
        if len(phases) == 1:
            from scripts.direct.phase_reservation import phase_directory as recorded_phase_directory

            return recorded_phase_directory(Path(session), phases[0]).resolve()
    raise ValueError("--fail-first needs one open phase or VERIFY_BATCH_PHASE_DIR")


def baseline_proof(packages: tuple[str, ...], fail_first: bool) -> BaselineProof | NoBaselineProof:
    if not fail_first:
        return NoBaselineProof.SKIP
    phase = phase_directory()
    revision = (phase / "phase_baseline").read_text().strip()
    if not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", revision):
        raise ValueError("phase_baseline must contain the full commit id")
    patch = phase / "regression.patch"
    if not patch.is_file() or patch.stat().st_size == 0:
        raise ValueError(f"{patch}: supply a patch that contains only regression tests")
    cases: list[RegressionCase] = []
    for row in (phase / "regression-tests").read_text().splitlines():
        if not row.strip():
            continue
        fields = row.split()
        if len(fields) != 2 or fields[0] not in packages or not re.fullmatch(r"[A-Za-z0-9_:]+", fields[1]):
            raise ValueError(f"invalid regression-tests row {row!r}; use a batch package and a test name")
        case = RegressionCase(*fields)
        if case in cases:
            raise ValueError(f"duplicate regression-tests row {row!r}")
        cases.append(case)
    if not cases:
        raise ValueError("regression-tests must name at least one regression")
    return BaselineProof(revision, patch, tuple(cases))


# Nextest uses 100 when a test ran and failed. Build and setup failures use other statuses.
NEXTEST_TEST_FAILED = 100


def run_baseline_tests(commands: BatchCommands, cases: Sequence[RegressionCase], cache_hit: Path) -> int:
    passed = True
    for case in cases:
        result = commands.verify_step("fail-first", ("test", case.package, "--filter", case.test, "--no-cache"),
                                      cache_hit)
        if result.status == NEXTEST_TEST_FAILED:
            verdict, reason = BatchStepResult(0, False), ""
        else:
            verdict = result
            reason = "fail-first passed on baseline" if result.status == 0 else f"baseline exit {result.status}"
        if not verdict.show("fail-first", case.package, reason):
            passed = False
    return int(not passed)


def run_baseline_copy(commands: BatchCommands, repo: Path, plan: BaselineProof, scratch: Path) -> bool:
    done = scratch / "baseline-done"
    cases = [f"{case.package}={case.test}" for case in plan.cases]
    script = Path(__file__).resolve()
    copies = script.parents[1] / "copies" / "copies.py"
    # The baseline gets the copy's target directory.
    command = ["env", "-u", "CARGO_TARGET_DIR", "-u", "CARGO_BUILD_BUILD_DIR",
               sys.executable, str(copies), "run", "--repo", str(repo), "--commit", plan.revision,
               "--", sys.executable, str(script), "--baseline",
               str(commands.verify), str(done), str(plan.patch), *cases,
               "--", *commands.route]
    cleanup = commands.cleanup
    if isinstance(cleanup, ForegroundCleanupLease):
        status = commands.run(command, {"COPIES_RUN_SCOPE_LOCK_FD": str(cleanup.descriptor)},
                              use=CleanupLeaseUse.SHARE_WITH_COORDINATOR)
    else:
        status = commands.run(command, {})
    baseline_status = done.read_text() if done.is_file() else ""
    if baseline_status != str(status):
        scope = ",".join(dict.fromkeys(case.package for case in plan.cases))
        return BatchStepResult(status, False).show("fail-first", scope, f"baseline copy exit {status}")
    return status == 0


def run_batch(commands: BatchCommands, repo: Path, packages: tuple[str, ...],
              lint: LintScope, baseline: BaselineProof | NoBaselineProof, scratch: Path) -> int:
    passed = True
    if isinstance(baseline, BaselineProof):
        passed = run_baseline_copy(commands, repo, baseline, scratch)
    scope = ",".join(packages) if lint is LintScope.PACKAGES else lint.value
    lint_args = ("lint", packages[0]) if lint is LintScope.PACKAGES else ("lint",)
    format_packages = scope if lint is LintScope.PACKAGES else ""
    hit = scratch / "cache-hit"
    formatting = commands.verify_step("format", lint_args, hit, format_packages=format_packages)
    before_tests = commands.tree_key(repo)
    if not formatting.show("format", scope, "tree check failed" if not before_tests else ""):
        passed = False

    stale = not before_tests
    for package in packages:
        test = commands.verify_step("test", ("test", package), hit)
        after_test = commands.tree_key(repo)
        if not test.show("test", package, "tree check failed" if not after_test else ""):
            passed = False
        stale |= after_test != before_tests
    finish = commands.verify_step("lint", lint_args, hit, format_packages=format_packages)
    after_lint = commands.tree_key(repo)
    stale |= after_lint != before_tests
    detail = "tree check failed" if not after_lint else "tests stale" if stale else ""
    if not finish.show("lint", scope, detail):
        passed = False
    print("verify.sh batch result " + ("green" if passed else "red"), flush=True)
    return int(not passed)


class BatchOptions(argparse.Namespace):
    packages: str = ""
    lint: str = ""
    fail_first: bool = False


def main(argv: list[str]) -> int:
    split = len(argv) - 1 - argv[::-1].index("--")
    args, route = argv[:split], argv[split + 1:]
    if args[0] == "--baseline":
        _, verify, marker, patch, *cases = args
        done = Path(marker)
        lease = os.environ.pop("COPIES_STEP_LOCK_FD", "")
        cleanup = (ForegroundCleanupLease(int(lease), ForegroundMarker.COORDINATED_COMMAND)
                   if lease else NoCleanupLease.NONE)
        commands = BatchCommands(Path(verify), route, cleanup=cleanup)
        if commands.run(["git", "apply", "--index", "--", patch], {}, lifetime=CommandLifetime.GROUP) != 0:
            return 2
        result = run_baseline_tests(commands, [RegressionCase(*case.split("=", 1)) for case in cases],
                                    done.with_name("baseline-cache-hit"))
        _ = done.write_text(str(result))
        return result
    repo, verify, scratch, lease, *words = args
    try:
        parser = argparse.ArgumentParser(prog="verify.sh batch", allow_abbrev=False)
        _ = parser.add_argument("--packages", required=True)
        _ = parser.add_argument("--lint", required=True, choices=[scope.value for scope in LintScope])
        _ = parser.add_argument("--fail-first", action="store_true")
        options = parser.parse_args(words, namespace=BatchOptions())
        packages = tuple(options.packages.split(","))
        if len(set(packages)) != len(packages) or not all(re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*", p) for p in packages):
            raise ValueError("--packages needs distinct package names separated by commas")
        baseline = baseline_proof(packages, options.fail_first)
        commands = BatchCommands(Path(verify), route, cleanup=ForegroundCleanupLease(int(lease)))
        return run_batch(commands, Path(repo), packages, LintScope(options.lint), baseline, Path(scratch))
    except (OSError, ValueError) as error:
        print(f"verify.sh batch: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
