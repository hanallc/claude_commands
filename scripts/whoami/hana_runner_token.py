"""Seal the Hana Linux runner token and publish the new credential.

Ctrl+C can cancel reseal before publication starts.
Publication completes or fails once it starts.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from scripts.lib import cancellation


CREDENTIAL_PATH = Path("/etc/nixos/modules/linux/hana-runner-token.cred")
ITEM = "op://hana/natedev - hana self-hosted ci runner token/credential"


class ResealRefused(Exception):
    """Report a fixed message without token data."""


def _seal() -> bytes:
    approval = subprocess.run(["sudo", "-v"], stdout=subprocess.DEVNULL, check=False)
    if approval.returncode:
        raise ResealRefused("Sudo authentication failed.")

    token = subprocess.run(["op", "read", "--no-newline", ITEM], stdout=subprocess.PIPE, check=False)
    if token.returncode:
        raise ResealRefused("Cannot read the runner token from 1Password.")
    if not token.stdout.strip():
        raise ResealRefused("1Password returned an empty runner token.")

    sealed = subprocess.run(
        ["sudo", "-n", "systemd-creds", "encrypt", "--with-key=host+tpm2", "--tpm2-pcrs=",
         "--name=hana-runner-token", "-", "-"],
        input=token.stdout, stdout=subprocess.PIPE, check=False)
    if sealed.returncode or not sealed.stdout:
        raise ResealRefused("Cannot seal the runner credential.")
    return sealed.stdout


def _publish(path: Path, sealed: bytes) -> None:
    with cancellation.defer_cleanup_signals():
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                    dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                _ = output.write(sealed)
                output.flush()
                os.fsync(output.fileno())
                os.fchmod(output.fileno(), 0o600)
            os.replace(temporary, path)
        except BaseException:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise
        finally:
            # Discard pending signals before the CLI reports the result.
            _ = signal.signal(signal.SIGINT, signal.SIG_IGN)
            _ = signal.signal(signal.SIGTERM, signal.SIG_IGN)


def main(argv: Sequence[str], *, path: Path = CREDENTIAL_PATH) -> int:
    if list(argv) != ["reseal"]:
        print("Usage: hana_runner_token.py reseal", file=sys.stderr)
        return 2
    try:
        _publish(path, _seal())
    except ResealRefused as error:
        print(error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Reseal interrupted.", file=sys.stderr)
        return 130
    except OSError:
        print("Cannot run the reseal command or write the sealed credential.", file=sys.stderr)
        return 1
    print(f"Replaced {path}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
