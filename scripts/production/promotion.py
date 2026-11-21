"""Verify the production's exact CI verdict before publishing main."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
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
        with tempfile.TemporaryDirectory(prefix="promotion-hook-") as folder:
            exact = Path(folder) / "checkout"
            _ = git(checkout, "-c", "core.hooksPath=/dev/null", "worktree", "add", "--detach", str(exact), self.sha)
            try:
                hook_path = ".claude/config/post_push.sh"
                local_hook = checkout / hook_path
                exact_hook = exact / hook_path
                if not exact_hook.resolve().is_relative_to(exact.resolve()):
                    raise land_main.LandError(f"{hook_path} resolves outside the approved checkout")
                tracked = git(checkout, "ls-files", "--", hook_path)
                if local_hook.is_file() and not exact_hook.exists() and not tracked:
                    exact_hook.parent.mkdir(parents=True, exist_ok=True)
                    _ = shutil.copyfile(local_hook, exact_hook)
                return land_main.land(exact, self.sha)
            finally:
                _ = git(checkout, "worktree", "remove", "--force", str(exact))


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


def publish[Result](approval: PromotionApproval, source: Path, branch: str, tip: str,
                    destinations: Callable[[ApprovedPromotion], Result]) -> Result:
    """Give the destinations permission to install and publish the approved commit."""
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", tip):
        raise checkpoint_gate.Stop("promote", "held", f"expected a full commit SHA, got {tip}",
                                   "specify the exact commit and retry promotion")
    with main_push_lock.main_push_lock(source):
        if git(source, "rev-parse", "--verify", f"{tip}^{{commit}}") != tip:
            raise checkpoint_gate.Stop("promote", "held", f"{tip} does not identify a commit",
                                       "specify the exact commit and retry promotion")
        require_approval(source, branch, tip, approval)
        return destinations(ApprovedPromotion(tip))
