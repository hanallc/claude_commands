#!/usr/bin/env python3
"""Make one approved configuration value live at once, then publish it to main and the Mac."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import os
from pathlib import Path
import platform
import shlex
import sys

from scripts.lib import conf, conf_reads, live_settings, main_publish
from scripts.lib.conf import Unusable, check
from scripts.build_hold.override_guard import refuse_overrides

SUBJECT = "tune("
FORWARD_LIMIT = main_publish.CommandTimeLimit(55)


class RequestedValueChange(argparse.Namespace):
    file: str = ""
    key: str = ""
    value: str = ""
    why: str = ""
    user_words: str = ""


def _one_line(name: str, value: str) -> None:
    if not value.strip() or "\n" in value or "\r" in value:
        raise main_publish.Refusal(f"{name} must be one nonempty line")


def _forward(arguments: list[str], ssh_program: str) -> int:
    command = ('"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/lib/tune.py" '
               + shlex.join(arguments))
    done = main_publish.ssh(command, ssh_program=ssh_program, host="natedev", time_limit=FORWARD_LIMIT)
    if isinstance(done, str):
        print(f"tune: natedev: {done}", file=sys.stderr)
        return 1
    print(done.stdout, end="")
    print(done.stderr, end="", file=sys.stderr)
    return done.returncode


def change(args: RequestedValueChange) -> int:
    """Make the value live here and on the Mac at once; main and the Mac pull follow."""
    source = conf.path(args.file).resolve()
    _, old = conf.editable_line(args.file, args.key)
    check(args.file, args.key, args.value)
    checkout = main_publish.checkout_of(source)
    if old == args.value:
        print(f"already {args.key}={args.value}")
    else:
        conf_reads.record(source.stem)
        message = (f"{SUBJECT}{args.file}): {args.key} {old} -> {args.value}\n\n"
                   + f"{args.why}\n\nUser-Approved: {args.user_words}")
        _ = live_settings.stage(live_settings.WaitingChange(args.file, args.key, args.value, message))
        print(f"live now: {args.key}={args.value}")
        for line in live_settings.start_readers(args.file):
            print(line)
        print(live_settings.write_mac(args.file, args.key, args.value))
    try:
        lines = live_settings.publish(checkout, wait=True) or []
    except (main_publish.Refusal, OSError) as error:
        print(f"tune: {' '.join(str(error).split())}", file=sys.stderr)
        print(f"tune: {args.key}={args.value} stays live; main does not have it; the notes timer tries again",
              file=sys.stderr)
        return 2
    for line in lines:
        if old != args.value or line.startswith("mac: queued"):
            print(line)
    return 0


def main(arguments: list[str] | None = None) -> int:
    refuse_overrides(Path(__file__), "lib/tune.py")
    argv = sys.argv[1:] if arguments is None else arguments
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("file", "key", "value"):
        _ = parser.add_argument(name)
    _ = parser.add_argument("--why", required=True)
    _ = parser.add_argument("--user-words", required=True)
    try:
        args = parser.parse_args(argv, namespace=RequestedValueChange())
        _one_line("--why", args.why)
        _one_line("--user-words", args.user_words)
        _one_line("file", args.file)
        _one_line("key", args.key)
        if "\n" in args.value or "\r" in args.value:
            raise main_publish.Refusal("value must fit on one line")
        if "#" in args.value or args.value != args.value.strip():
            raise main_publish.Refusal("value must have no comment marker or outer whitespace")
        if platform.system() == "Darwin":
            return _forward(argv, os.environ.get("ZONE_SSH") or "ssh")
        return change(args)
    except SystemExit as error:
        return error.code if isinstance(error.code, int) else 2
    except (Unusable, main_publish.Refusal, OSError, UnicodeError) as error:
        print(f"tune: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
