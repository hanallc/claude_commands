"""Find the original checkout for a pinned Verify script."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
import fcntl
import os
from pathlib import Path
import sys


PIN_ENV = ("VERIFY_SCRIPT_PIN", "VERIFY_SCRIPT_REPO", "VERIFY_SCRIPT_COMMIT")
VERIFY_ENTRY = Path("scripts/direct/verify.sh")


class UnpinnedVerify(Enum):
    NO_PIN = "no Verify script pin"


@dataclass(frozen=True)
class VerifyScriptPin:
    folder: Path
    repository: Path
    commit: str

    @property
    def attached_owner(self) -> Path:
        return self.folder / ".verify-owner"

    @property
    def detached_marker(self) -> Path:
        return self.folder / ".detached"

    def report_retained(self, cause: str) -> None:
        print(f"kept for a manual removal list: {self.folder} (verify, {cause})", file=sys.stderr)

    def remove(self) -> None:
        """Remove the private script copy when its owner ends."""
        import shutil

        try:
            with (self.folder / ".sweep-lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                shutil.rmtree(self.folder)
        except OSError as error:
            if not isinstance(error, FileNotFoundError) or self.folder.exists():
                self.report_retained(f"cleanup lock or removal failed: {error.strerror or error}")


def from_code_root(root: Path, environment: Mapping[str, str] = os.environ) -> VerifyScriptPin | UnpinnedVerify:
    """Use the pin if its source stamp matches the caller's paths and commit."""
    folder, repository, commit = (environment.get(key, "") for key in PIN_ENV)
    if (folder and repository and commit and root == Path(folder)
            and root.name.startswith("verify-scripts.") and Path(repository).is_absolute()):
        stamp = root / ".verify-source"
        expected = os.fsencode(repository) + b"\0" + os.fsencode(commit) + b"\0"
        try:
            if stamp.is_file() and not stamp.is_symlink() and stamp.stat().st_uid == os.getuid():
                with stamp.open("rb") as source:
                    if source.read(len(expected) + 1) == expected:
                        return VerifyScriptPin(root, Path(repository), commit)
        except OSError:
            pass
    return UnpinnedVerify.NO_PIN


