"""Guard tracked files against fixed producer names."""
from __future__ import annotations

import json
import re
import subprocess
import unittest
from collections import defaultdict, deque
from pathlib import Path
from typing import TypedDict, cast

ROOT = Path(__file__).resolve().parents[2]
NAMES = "scripts/production/producer_name_guard_names.txt"
EXCEPTIONS = "scripts/production/producer_name_guard_exceptions.json"
CAMEL_BOUNDARY = re.compile(r"(?<=[a-z])(?=[A-Z])")
TOKEN = re.compile(r"(?:[^\W_]|-)+")


class ExceptionLine(TypedDict):
    path: str
    line: str
    reason: str


def has_producer_name(line: str, names: set[str]) -> bool:
    """Match whole names, possessives and names in identifiers."""
    return any(part.casefold() in names
               for match in TOKEN.finditer(line)
               for token in (match[0], *match[0].split("-"))
               for part in (token, *CAMEL_BOUNDARY.split(token)))


class ProducerNameGuardTests(unittest.TestCase):
    def test_hyphenated_names_match_complete_tokens(self) -> None:
        names = {"fixture-new", "fixture-news"}
        for line in ("docs/fixture-new/README.md", "send_Fixture-New_notice", "sendFixture-newNotice", "fixture-new's", "fixture-news"):
            with self.subTest(line=line):
                self.assertTrue(has_producer_name(line, names))
        for line in ("fixture", "new", "fixture_new", "fixture-newer", "other-fixture-new", "fixture-new-extra"):
            with self.subTest(line=line):
                self.assertFalse(has_producer_name(line, names))

    def test_tracked_files_use_staff_ids_or_name_fields(self) -> None:
        words = (ROOT / NAMES).read_text(encoding="utf-8").splitlines()
        names = {name.casefold() for name in words if name}
        self.assertTrue(names, "No producer names to check.")
        names |= {name + "s" for name in names}
        entries = cast(list[ExceptionLine], json.loads((ROOT / EXCEPTIONS).read_text(encoding="utf-8")))
        exceptions: dict[tuple[str, str], deque[str]] = defaultdict(deque)
        for entry in entries:
            exceptions[entry["path"], entry["line"]].append(entry["reason"])

        tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT, text=True).split("\0")
        failures: list[str] = []
        for relative in sorted(set(tracked) - {"", NAMES, EXCEPTIONS}):
            path = ROOT / relative
            if path.is_symlink() or not path.is_file():
                continue
            try:
                source = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            folded = source.casefold()
            if not any(name in folded for name in names):
                continue
            for number, line in enumerate(source.splitlines(), 1):
                if not has_producer_name(line, names):
                    continue
                allowed = exceptions.get((relative, line))
                if allowed:
                    _ = allowed.popleft()
                else:
                    failures.append(f"{relative}:{number}: {line}")
        failures.extend(f"{path}: unused exception ({reason}): {line}"
                        for (path, line), reasons in exceptions.items() for reason in reasons)
        self.assertFalse(failures, "\n".join(failures))
