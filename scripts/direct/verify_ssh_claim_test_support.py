"""Record exact SSH claim exclusions for private Verify fixtures."""

from dataclasses import dataclass
from pathlib import Path

from scripts.build_queue import admission_protocol as wire
from scripts.mac_test import claim_state
from scripts.mac_test.mac_test import scopes_overlap
from scripts.mac_test.offload import pending_start_key


@dataclass(frozen=True)
class RetainedClaimExclusion:
    """An unreleased claim excludes its provider scope."""

    scope: claim_state.ClaimScope


def claim_exclusion_directory(root: Path, target: str) -> Path:
    return root / f"provider-claim-{target}"


def claim_exclusion_path(root: Path, target: str, identity: claim_state.ClaimIdentity) -> Path:
    return claim_exclusion_directory(root, target) / f"{pending_start_key(identity)}.json"


def exclude_retained_claim(root: Path, target: str, scope: claim_state.ClaimScope) -> bool:
    for path in claim_exclusion_directory(root, target).glob("*.json"):
        try:
            exclusion = wire.parse_record(path.read_text(), RetainedClaimExclusion)
        except FileNotFoundError:
            continue
        if scopes_overlap(exclusion.scope, scope):
            return True
    return False
