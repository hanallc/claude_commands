"""Let a Linux process adopt its orphaned descendants."""
from __future__ import annotations

import ctypes
import sys
from typing import cast


def descendant_adoption_available() -> bool:
    return sys.platform == "linux" and hasattr(ctypes.CDLL(None), "prctl")


def adopt_descendants() -> bool:
    """Enable orphan adoption. Return false where prctl is unavailable."""
    if not descendant_adoption_available():
        return False
    libc = ctypes.CDLL(None, use_errno=True)
    # PR_SET_CHILD_SUBREAPER is 36 in the Linux ABI.
    if cast(int, libc.prctl(36, 1, 0, 0, 0)) != 0:
        raise OSError(ctypes.get_errno(), "cannot adopt descendants")
    return True
