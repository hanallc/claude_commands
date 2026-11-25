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
import re
import sys

from scripts.lib import conf, live_settings, main_publish
from scripts.build_hold.override_guard import refuse_overrides

SUBJECT = "tune("


class RequestedValueChange(argparse.Namespace):
    file: str = ""
    key: str = ""
    value: str = ""
    why: str = ""
    user_words: str = ""


def _one_line(name: str, value: str) -> None:
    if not value.strip() or "\n" in value or "\r" in value:
        raise main_publish.Refusal(f"{name} must be one nonempty line")


def change(args: RequestedValueChange) -> int:
    """Make the value live here and on the Mac at once; main and the Mac pull follow."""
    source = conf.path(args.file).resolve()
    checkout = main_publish.checkout_of(source)

    def describe(old: str) -> str:
        return (f"{SUBJECT}{args.file}): {args.key} {old} -> {args.value}\n\n"
                + f"{args.why}\n\nUser-Approved: {args.user_words}")

    saved = live_settings.stage_save(live_settings.WaitingChange(args.file, args.key, args.value, ""),
                                     describe=describe)
    if isinstance(saved, live_settings.UnchangedSave):
        print(f"already {args.key}={args.value}")
    else:
        print(f"live now: {args.key}={args.value}")
        if args.file in ("hana_aws", "build_targets", "build_folders"):
            from scripts.aws_target import hana_render

            if hana_render.affects_guest_config(args.key, source):
                print("The Hana image is now stale. Build and register a current Hana image.")
        for line in live_settings.start_readers(args.file):
            print(line)
        print(live_settings.write_mac(args.file, args.key, args.value, order=saved.order))
    try:
        lines = live_settings.publish(checkout, wait=True) or []
    except (main_publish.Refusal, OSError) as error:
        print(f"tune: {' '.join(str(error).split())}", file=sys.stderr)
        print(f"tune: {args.key}={args.value} stays live; main does not have it; the notes timer tries again",
              file=sys.stderr)
        return 2
    for line in lines:
        if isinstance(saved, live_settings.StagedSave) or line.startswith("mac: queued"):
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
        if re.search(r"\s#", args.value) or args.value != args.value.strip():
            raise main_publish.Refusal("value must have no whitespace comment marker or outer whitespace")
        if platform.system() == "Darwin":
            return main_publish.forward("lib/tune.py", argv, label="tune", ssh_program=os.environ.get("ZONE_SSH") or "ssh")
        return change(args)
    except SystemExit as error:
        return error.code if isinstance(error.code, int) else 2
    except (conf.InvalidKey, conf.Unusable, main_publish.Refusal, OSError, UnicodeError) as error:
        print(f"tune: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
