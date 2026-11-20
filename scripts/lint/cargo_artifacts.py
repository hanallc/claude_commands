"""Read Cargo artifact messages for prebuild and Verify."""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys
from typing import cast


@dataclass(frozen=True)
class ExecutablePath:
    path: str


@dataclass(frozen=True)
class NoExecutable:
    """Cargo did not report an executable for this artifact."""


@dataclass(frozen=True)
class CompilerArtifact:
    package: str
    target: str
    kinds: frozenset[str]
    compiled_as_test: bool
    executable: ExecutablePath | NoExecutable
    filenames: tuple[str, ...]


def package_name(package_id: str) -> str:
    """Read the name from the old or current Cargo package ID format."""
    old_form = re.fullmatch(r"(\S+) \S+ \(.*\)", package_id)
    if old_form is not None:
        return old_form.group(1)
    source, _, fragment = package_id.partition("#")
    if "@" in fragment:
        return fragment.rpartition("@")[0]
    return source.partition("?")[0].rstrip("/").rpartition("/")[2]


def strings(value: object) -> Iterator[str]:
    if isinstance(value, list):
        yield from (word for word in cast(list[object], value) if isinstance(word, str))


def compiler_artifacts(lines: Iterable[str]) -> Iterator[CompilerArtifact]:
    """Read each compiler artifact and ignore other Cargo output."""
    for line in lines:
        try:
            message: object = json.loads(line)  # pyright: ignore[reportAny]
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue
        fields = cast(dict[str, object], message)
        package_id, target = fields.get("package_id"), fields.get("target")
        if (fields.get("reason") != "compiler-artifact" or not isinstance(package_id, str)
                or not isinstance(target, dict)):
            continue
        target_fields = cast(dict[str, object], target)
        name = target_fields.get("name")
        profile = fields.get("profile")
        compiled_as_test = isinstance(profile, dict) and cast(dict[str, object], profile).get("test") is True
        executable = fields.get("executable")
        yield CompilerArtifact(package_name(package_id), name if isinstance(name, str) else "",
                               frozenset(strings(target_fields.get("kind"))), compiled_as_test,
                               ExecutablePath(executable) if isinstance(executable, str) else NoExecutable(),
                               tuple(strings(fields.get("filenames"))))


def runnable_harnesses(lines: Iterable[str], package: str, target: str, kinds: frozenset[str]) -> set[str]:
    paths: set[str] = set()
    for artifact in compiler_artifacts(lines):
        if (artifact.package != package or artifact.target != target or artifact.kinds != kinds
                or not artifact.compiled_as_test or not isinstance(artifact.executable, ExecutablePath)):
            continue
        path = artifact.executable.path
        if "\n" in path or "\r" in path or "\0" in path:
            continue
        if os.path.isabs(path) and os.path.isfile(path) and os.access(path, os.X_OK):
            paths.add(path)
    return paths


def main(arguments: list[str]) -> int:
    capture, package, target, kinds = arguments
    try:
        with Path(capture).open(encoding="utf-8", errors="replace") as lines:
            paths = runnable_harnesses(lines, package, target, frozenset(kinds.split(",")))
    except OSError as error:
        print(f"verify.sh: cannot read Cargo artifacts: {error}", file=sys.stderr)
        return 1
    if len(paths) != 1:
        print(f"verify.sh: expected one runnable test harness for {package}/{target}, found {len(paths)}",
              file=sys.stderr)
        return 1
    path, = paths
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
