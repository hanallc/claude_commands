"""Verify the production's exact CI verdict before publishing main."""

from __future__ import annotations

try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from scripts.lib import land_main, main_push_lock, waiting_rows
from scripts.production import checkpoint_gate, production_doc, staff, unit_lookup
from scripts.production.production_names import CARGO_LINER_PRODUCTION


@dataclass(frozen=True)
class CollectedCi:
    document: Path


@dataclass(frozen=True)
class LifecycleCi:
    document: Path
    verdict: str


@dataclass(frozen=True)
class LifecycleNoCi:
    document: Path


@dataclass(frozen=True)
class FastTrackInstall:
    """Install a fast-track commit after its separate gate published it."""


type PromotionApproval = CollectedCi | LifecycleCi | LifecycleNoCi | FastTrackInstall


@dataclass(frozen=True)
class ApprovedPromotion:
    """The exact commit that this operation permits at each destination."""

    sha: str

    def install(self, checkout: Path, expected_head: str) -> None:
        waiting_rows.move_to(checkout, self.sha, fast_forward=True, expected_head=expected_head)

    def land(self, checkout: Path) -> land_main.Landed:
        if git(checkout, "rev-parse", "HEAD") == self.sha:
            return land_main.land(checkout, self.sha)
        with land_main.exact_hook_checkout(checkout, self.sha) as exact:
            return land_main.land(exact, self.sha, retry_source=checkout)


@dataclass(frozen=True)
class PublishedMain:
    """The approved commit and the result of publication to main."""

    sha: str


def git(checkout: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-C", str(checkout), *arguments],
                            capture_output=True, text=True, check=False)
    if result.returncode:
        raise checkpoint_gate.Stop("promote", "failed", result.stderr.strip() or f"git {arguments[0]} failed",
                                   "inspect Git and retry promotion")
    return result.stdout.strip()


def require_approval(source: Path, branch: str, tip: str, approval: PromotionApproval) -> None:
    if isinstance(approval, FastTrackInstall):
        return
    document = approval.document
    try:
        owner = unit_lookup.doc_owner(document, staff.snapshot())
        if not isinstance(owner.doc, staff.LiveDoc):
            raise production_doc.Refusal(f"{document}: expected a live production document")
        paths = production_doc.resolve_production_paths(document)
        enrolled = isinstance(paths, production_doc.EnrolledPaths)
        if enrolled:
            lines = document.read_text(encoding="utf-8").splitlines()
            if (paths.integration != source.resolve()
                    or production_doc.production_field(lines, "Merge branch") != branch):
                raise production_doc.Refusal(f"{document}: source is not the production integration branch")
    except (OSError, ValueError, production_doc.Refusal) as error:
        raise checkpoint_gate.Stop("promote", "held", str(error), "repair the production identity and retry") from error
    if enrolled and owner.name == CARGO_LINER_PRODUCTION:
        if isinstance(approval, LifecycleNoCi):
            raise checkpoint_gate.Stop("promote", "held", "this production requires CI on the integration tip",
                                       "collect CI and retry promotion")
        checkpoint_gate.require_promotion_ci(owner, tip, "cargo-liner")
        integration = f"refs/heads/{branch}"
        local = git(source, "rev-parse", "--verify", integration)
        remote = git(source, "ls-remote", "origin", integration)
        if local != tip or remote != f"{tip}\t{integration}":
            raise checkpoint_gate.Stop("promote", "held", f"collect cargo-liner CI on the integration tip before promoting {tip}",
                                       "collect CI and retry promotion")
    elif isinstance(approval, CollectedCi):
        checkpoint_gate.require_promotion_ci(owner, tip, "standard")
    elif isinstance(approval, LifecycleCi):
        if not re.fullmatch(r"[0-9a-f]{7,64}", approval.verdict):
            raise checkpoint_gate.Stop("promote", "held", "CI verdict must identify a commit by SHA",
                                       "collect CI and retry promotion")
        verdict = git(source, "rev-parse", "--verify", "--end-of-options", f"{approval.verdict}^{{commit}}")
        if verdict != tip:
            raise checkpoint_gate.Stop("promote", "held", f"CI verdict {verdict} differs from promotion commit {tip}",
                                       "collect CI and retry promotion")


def is_main_branch(branch: str) -> bool:
    return branch.removeprefix("refs/heads/") == "main"


def integration_push_script(production: production_doc.ProductionDocument) -> str:
    if is_main_branch(production.merge_branch) and isinstance(production.merge_checkout, production_doc.LegacyProduction):
        return ""
    return str(Path(__file__).resolve())


def require_exact_commit(source: Path, tip: str) -> None:
    if (not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", tip)
            or git(source, "rev-parse", "--verify", f"{tip}^{{commit}}") != tip):
        raise checkpoint_gate.Stop("promote", "held", f"expected a full commit SHA, got {tip}",
                                   "specify the exact commit and retry promotion")


def push_integration(source: Path, tip: str, branch: str) -> None:
    """Push the exact commit to a named integration branch."""
    destination = f"refs/heads/{branch.removeprefix('refs/heads/')}"
    if is_main_branch(branch):
        raise checkpoint_gate.Stop("push", "held", f"{destination} is not an integration branch",
                                   "publish through production promotion")
    try:
        require_exact_commit(source, tip)
        _ = git(source, "check-ref-format", destination)
        _ = git(source, "push", "origin", f"{tip}:{destination}")
    except checkpoint_gate.Stop as error:
        raise checkpoint_gate.Stop("push", error.state, error.detail, "repair the integration push and retry") from error


def publish(approval: PromotionApproval, source: Path, branch: str, tip: str,
                    destinations: Callable[[ApprovedPromotion], None]) -> PublishedMain:
    """Return the approved SHA after the destinations publish it."""
    with main_push_lock.main_push_lock(source):
        require_exact_commit(source, tip)
        require_approval(source, branch, tip, approval)
        destinations(ApprovedPromotion(tip))
        return PublishedMain(tip)


def publish_legacy_main(source: Path, tip: str,
                                destinations: Callable[[ApprovedPromotion], None]) -> PublishedMain:
    """Complete publication for the legacy direct-main production."""
    with main_push_lock.main_push_lock(source):
        require_exact_commit(source, tip)
        destinations(ApprovedPromotion(tip))
        return PublishedMain(tip)


class IntegrationPushArguments(argparse.Namespace):
    checkout: Path = Path()
    commit: str = ""
    branch: str = ""


def main() -> int:
    parser = argparse.ArgumentParser(description=push_integration.__doc__)
    _ = parser.add_argument("--checkout", type=Path, required=True)
    _ = parser.add_argument("--commit", required=True)
    _ = parser.add_argument("--branch", required=True)
    args = parser.parse_args(namespace=IntegrationPushArguments())
    try:
        push_integration(args.checkout, args.commit, args.branch)
    except checkpoint_gate.Stop as error:
        print(error.detail, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
