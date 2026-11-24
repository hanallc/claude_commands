"""Check the repository config files against the common form rules."""

from collections.abc import Iterator, Sequence
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from pathlib import Path
import re
import subprocess
import tempfile
from typing import NamedTuple
import unittest

from scripts.lib import conf, config_form_exceptions as exceptions
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class Violation(NamedTuple):
    """Report a rule failure. Line zero identifies a file format failure."""

    path: Path
    line_number: int
    rule: str
    line: str

    def diagnostic(self) -> str:
        return f"{self.path}:{self.line_number}: {self.rule}: {self.line}"


class _KeyRow(NamedTuple):
    key: str
    value: str


def _conf_lines(root: Path, files: Sequence[Path]) -> Iterator[tuple[Path, list[str]]]:
    for path in files:
        if path.suffix == ".conf":
            yield path, (root / path).read_text(encoding="utf-8").splitlines()


def _without_comment(line: str) -> str:
    return re.split(r"(?:^|\s)#", line, maxsplit=1)[0].strip()


def _key_row(line: str) -> _KeyRow | None:
    content = _without_comment(line)
    key, equals, value = content.partition("=")
    if equals and not content.startswith("["):
        return _KeyRow(key.strip(), value.strip())
    return None


def _is_section(line: str) -> bool:
    return re.fullmatch(r"\[[^\[\]]+\]", _without_comment(line)) is not None


def _machine_parts(root: Path) -> set[str]:
    machines_path = root / "config/machines.conf"
    targets_path = root / "config/build_targets.conf"
    machines = conf.settings(machines_path.read_text(encoding="utf-8")) if machines_path.is_file() else {}
    targets = conf.settings(targets_path.read_text(encoding="utf-8")) if targets_path.is_file() else {}

    aliases = machines["aliases"].value.split() if "aliases" in machines else []
    parts = set(aliases)
    for alias in aliases:
        hosts_key = f"{alias}.hosts"
        if hosts_key in machines:
            parts.update(machines[hosts_key].value.split())
    for key in ("targets", "pinned_targets"):
        if key in targets:
            parts.update(name.strip() for name in targets[key].value.split(",") if name.strip())
    return parts


def _is_number(value: str) -> bool:
    try:
        return Decimal(value).is_finite()
    except InvalidOperation:
        return False


def check_format(root: Path, files: Sequence[Path]) -> list[Violation]:
    # File formats do not depend on the file contents.
    return [Violation(path, 0, "format", path.name) for path in files if (root / path).suffix != ".conf"]


def check_key_form(root: Path, files: Sequence[Path]) -> list[Violation]:
    machine_parts = _machine_parts(root)
    violations: list[Violation] = []
    for path, lines in _conf_lines(root, files):
        for number, line in enumerate(lines, start=1):
            row = _key_row(line)
            if row is None:
                continue
            if any(part not in machine_parts and not re.fullmatch(r"[a-z0-9][a-z0-9_]*", part)
                   for part in row.key.split(".")):
                violations.append(Violation(path, number, "key form", line))
    return violations


def check_sections(root: Path, files: Sequence[Path]) -> list[Violation]:
    return [Violation(path, number, "sections", line)
            for path, lines in _conf_lines(root, files)
            for number, line in enumerate(lines, start=1) if _is_section(line)]


def check_comment_limits(root: Path, files: Sequence[Path]) -> list[Violation]:
    violations: list[Violation] = []
    for path, lines in _conf_lines(root, files):
        block_has_comment = False
        previous = ""
        for number, line in enumerate(lines, start=1):
            row = _key_row(line)
            if row is None:
                block_has_comment = line.lstrip().startswith("#")
            else:
                if not block_has_comment or (
                    not previous.startswith(("# whole:", "# range:")) and _is_number(row.value)
                ):
                    violations.append(Violation(path, number, "comment and limit", line))
            previous = line.strip()
    return violations


def check_blank_lines(root: Path, files: Sequence[Path]) -> list[Violation]:
    return [Violation(path, number, "blank line", line)
            for path, lines in _conf_lines(root, files)
            for number, (previous, line) in enumerate(pairwise(lines), start=2)
            if (line.lstrip().startswith("#") or _is_section(line)) and _key_row(previous) is not None]


class ConfigFormTests(unittest.TestCase):
    def test_repository_form(self) -> None:
        root = Path(__file__).resolve().parents[2]
        tracked = subprocess.run(
            ["git", "ls-files", "-z", "config/"], cwd=root, check=True, capture_output=True,
            text=True, timeout=TEST_HANG_GUARD_SECS,
        )
        files = [Path(path) for path in tracked.stdout.split("\0") if path]

        # Collect violations before applying exceptions. This also finds stale exceptions.
        format_violations = check_format(root, files)
        key_violations = check_key_form(root, files)
        all_violations = (
            ("format", format_violations, exceptions.DATA_FILES + exceptions.PENDING_TOML_FILES),
            ("key form", key_violations, exceptions.KEY_FORM_FILES),
            ("sections", check_sections(root, files), exceptions.SECTION_FILES),
            ("comment and limit", check_comment_limits(root, files), exceptions.COMMENT_LIMIT_FILES),
            ("blank line", check_blank_lines(root, files), exceptions.BLANK_LINE_FILES),
        )
        covered: set[Violation] = set()
        problems: list[str] = []
        for rule, violations, allowed_files in all_violations:
            for entry in allowed_files:
                path = Path(entry.path)
                matches = [violation for violation in violations if violation.path == path]
                if matches:
                    covered.update(matches)
                elif not (rule == "blank line" and path == Path("config/the_list.conf") and path not in files):
                    problems.append(f"{path}: {rule}: stale file exception")

        for suffix in exceptions.DATA_SUFFIXES:
            matches = [violation for violation in format_violations if violation.path.suffix == suffix]
            if matches:
                covered.update(matches)
            else:
                problems.append(f"{suffix}: format: stale data suffix exception")

        for entry in exceptions.KEY_FORM_KEYS:
            path = Path(entry.path)
            matches = [violation for violation in key_violations
                       if violation.path == path and (row := _key_row(violation.line)) is not None
                       and row.key == entry.key]
            if matches:
                covered.update(matches)
            else:
                problems.append(f"{entry.path}: key form: stale key exception for {entry.key}")

        problems.extend(violation.diagnostic() for _, violations, _ in all_violations
                        for violation in violations if violation not in covered)
        if problems:
            self.fail("\n".join(problems))

    def test_planted_violations(self) -> None:
        large_number_row = f"amount={'9' * 310}"
        cases = (
            (check_format, "bad.ini", "value=text\n", 0, "format", "bad.ini"),
            (check_key_form, "example.conf", "# Setting\nfeature.BadName=value\n", 2,
             "key form", "feature.BadName=value"),
            (check_sections, "example.conf", "# Setting\n[feature] # Heading\n", 2,
             "sections", "[feature] # Heading"),
            (check_comment_limits, "example.conf", "# Setting\nfirst=text\n\nsecond=text\n", 4,
             "comment and limit", "second=text"),
            (check_comment_limits, "example.conf", "# whole: 1..5\nfirst=4\nsecond=-2.5e0 # Value\n", 3,
             "comment and limit", "second=-2.5e0 # Value"),
            (check_comment_limits, "example.conf", f"# Amount\n{large_number_row}\n", 2,
             "comment and limit", large_number_row),
            (check_blank_lines, "example.conf", "first=text # Value\n# Next setting\nsecond=text\n", 2,
             "blank line", "# Next setting"),
            (check_blank_lines, "example.conf", "first=text\n\n# Setting\nsecond=text\n[feature]\n", 5,
             "blank line", "[feature]"),
        )
        for check, name, source, number, rule, line in cases:
            with self.subTest(rule=rule, line=line), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                path = Path("config") / name
                (root / path).parent.mkdir()
                _ = (root / path).write_text(source, encoding="utf-8")
                files = [path]
                if path.suffix == ".conf":
                    data = path.with_suffix(".md")
                    _ = (root / data).write_text(source, encoding="utf-8")
                    files.append(data)
                self.assertEqual(check(root, files), [Violation(path, number, rule, line)])

    def test_key_form_accepts_names_from_fixture_config(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "config").mkdir()
            _ = (root / "config/machines.conf").write_text(
                "aliases=build-mac\nbuild-mac.hosts=Mac-Host\n", encoding="utf-8",
            )
            _ = (root / "config/build_targets.conf").write_text(
                "targets=cloud-x, cloud-y\npinned_targets=cloud-z\n", encoding="utf-8",
            )
            path = Path("config/example.conf")
            _ = (root / path).write_text(
                "build-mac.mode=value\nmode.Mac-Host=value\nmode.cloud-x.cloud-y.cloud-z=value\n"
                + "feature.2_mode=value\nfeature.Unknown-Host=value\n",
                encoding="utf-8",
            )
            self.assertEqual(check_key_form(root, [path]), [
                Violation(path, 5, "key form", "feature.Unknown-Host=value"),
            ])
