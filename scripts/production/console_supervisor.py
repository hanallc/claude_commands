"""Stop a console process group when its checker exits."""
from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
from collections.abc import Sequence


def supervise(checker_read: int, command: Sequence[str]) -> int:
    """Run the child in this group and watch the checker's pipe."""
    with subprocess.Popen(command) as child:
        # Wait for the pipe to close. Check for child exit between reads.
        while True:
            status = child.poll()
            timeout = 0.1 if status is None else 0
            if select.select([checker_read], [], [], timeout)[0] and not os.read(checker_read, 1):
                os.killpg(os.getpgrp(), signal.SIGKILL)
            if status is not None:
                return status if status >= 0 else 128 - status


if __name__ == "__main__":
    raise SystemExit(supervise(int(sys.argv[1]), sys.argv[2:]))
