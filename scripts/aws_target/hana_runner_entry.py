"""Start the transferred Hana runner and write its reply."""
from __future__ import annotations

from contextlib import redirect_stdout, suppress
import json
from pathlib import Path
import sys
import traceback


def write_refusal(reason: str) -> None:
    """Use the refusal record shared by command and source update replies."""
    _ = sys.stdout.buffer.write(json.dumps({'refused': reason}, separators=(',', ':')).encode())
    sys.stdout.buffer.flush()


def main() -> int:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        with redirect_stdout(sys.stderr):
            from scripts.aws_target import hana_runner
        return hana_runner.main()
    except SystemExit as error:
        if error.code == 0:
            raise
        write_refusal('The Hana runner refused its arguments')
    except Exception as error:
        write_refusal(f'The Hana runner stopped: {type(error).__name__}')
        with suppress(Exception):
            traceback.print_exc()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
