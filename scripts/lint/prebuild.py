#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Build the artifacts that a workspace's tests need, then run the test command.

Usage: prebuild.py --manifest <Cargo.toml> -- [holder words...] cargo nextest run <arguments...>

The workspace declares each artifact in its root Cargo.toml:

    [workspace.metadata.dev.prebuild.<package>]
    target = "<target triple>"
    env = "<variable name>"
    for = ["<package whose tests need it>", ...]
    platforms = ["linux"]  # optional: the platforms that build it; default all


invoke.sh puts this script in front of a nextest step, so the build runs on the
host that runs the tests, in the same admission. When the nextest arguments
select a `for` package, or the whole workspace, this runs
`cargo build --release -p <package> --target <target>` and sets `<env>` to the
artifact path. On a platform that `platforms` leaves out, the tests run
without the build. Cargo's fingerprints make an unchanged build a no-op. A failed
build exits with cargo's status, and the tests do not run.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import cast

ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
FILTER_OPTIONS = ("-E", "--filter-expr", "--filterset")
PACKAGE_OPTIONS = ("-p", "--package")
PACKAGE_NAME = re.compile(r"[A-Za-z0-9_-]+")
PACKAGE_ATOM = re.compile(r"package\(\s*=?([A-Za-z0-9_-]+)\s*\)")
LIBRARY_SUFFIXES = (".rlib", ".rmeta", ".d")
PLATFORMS = frozenset({"linux", "macos"})


class DeclarationError(ValueError):
    """The workspace's prebuild declaration is not valid."""


@dataclass(frozen=True)
class Prebuild:
    """One declared artifact: the package, its target, its variable and its test packages."""

    package: str
    target: str
    env: str
    for_packages: frozenset[str]
    platforms: frozenset[str] = PLATFORMS


@dataclass(frozen=True)
class WholeWorkspace:
    """The test run selects every package."""


@dataclass(frozen=True)
class SelectedPackages:
    """The test run selects only these packages."""

    names: frozenset[str]


Selection = WholeWorkspace | SelectedPackages


@dataclass(frozen=True)
class ArtifactFound:
    """The build made one linked artifact at this path."""

    path: str


@dataclass(frozen=True)
class ArtifactMissing:
    """The build made no single linked artifact; the reason says what it made."""

    reason: str


Artifact = ArtifactFound | ArtifactMissing


def table(value: object, where: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise DeclarationError(f"{where} must be a table")
    return cast(dict[str, object], value)


def text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise DeclarationError(f"{where} must be a non-empty string")
    return value


def read_prebuilds(manifest: Path) -> tuple[Prebuild, ...]:
    """Read `workspace.metadata.dev.prebuild`; a manifest without it declares none."""
    with manifest.open("rb") as source:
        value: object = tomllib.load(source)
    for key in ("workspace", "metadata", "dev", "prebuild"):
        value = table(value, "Cargo.toml").get(key)
        if value is None:
            return ()
    prebuilds: list[Prebuild] = []
    for package, entry in sorted(table(value, "workspace.metadata.dev.prebuild").items()):
        where = f"workspace.metadata.dev.prebuild.{package}"
        fields = table(entry, where)
        unknown = sorted(set(fields) - {"target", "env", "for", "platforms"})
        if unknown:
            raise DeclarationError(f"{where}: unknown key {unknown[0]}")
        env = text(fields.get("env"), f"{where}.env")
        if ENV_NAME.fullmatch(env) is None:
            raise DeclarationError(f"{where}.env must be a variable name")
        names = fields.get("for")
        if not isinstance(names, list) or not names:
            raise DeclarationError(f"{where}.for must be a non-empty list of package names")
        for_packages = frozenset(text(name, f"{where}.for") for name in cast(list[object], names))
        platforms = fields.get("platforms", sorted(PLATFORMS))
        if not isinstance(platforms, list) or not platforms:
            raise DeclarationError(f"{where}.platforms must be a non-empty list of linux and macos")
        platform_names = frozenset(text(name, f"{where}.platforms") for name in cast(list[object], platforms))
        if platform_names - PLATFORMS:
            raise DeclarationError(f"{where}.platforms must be a non-empty list of linux and macos")
        prebuilds.append(Prebuild(package, text(fields.get("target"), f"{where}.target"), env, for_packages,
                                  platform_names))
    return tuple(prebuilds)


def option_values(words: tuple[str, ...], names: tuple[str, ...]) -> list[str]:
    """Collect each value of the named options, before any `--`."""
    values: list[str] = []
    boundary = words.index("--") if "--" in words else len(words)
    for index, word in enumerate(words[:boundary]):
        if word in names and index + 1 < boundary:
            values.append(words[index + 1])
        values.extend(word[len(name) + 1:] for name in names if word.startswith(name + "="))
    return values


def top_level_parts(expression: str, symbols: str, word: str) -> list[str]:
    """Split a filterset at the operators outside every parenthesis."""
    parts: list[str] = []
    depth = start = index = 0
    while index < len(expression):
        character = expression[index]
        depth += {"(": 1, ")": -1}.get(character, 0)
        operator = re.match(rf"\s+{word}\s+", expression[index:]) if depth == 0 else None
        if depth == 0 and character in symbols:
            parts.append(expression[start:index])
            index += 1
            start = index
        elif operator is not None:
            parts.append(expression[start:index])
            index += operator.end()
            start = index
        else:
            index += 1
    parts.append(expression[start:])
    return [part.strip() for part in parts]


def without_outer_parentheses(expression: str) -> str:
    expression = expression.strip()
    while expression.startswith("("):
        depth = 0
        for index, character in enumerate(expression):
            depth += {"(": 1, ")": -1}.get(character, 0)
            if depth == 0:
                break
        else:
            return expression
        if index != len(expression) - 1:
            return expression
        expression = expression[1:-1].strip()
    return expression


def expression_selection(expression: str) -> Selection:
    """Name the packages a filterset can select; any other form selects the workspace.

    `&` binds tighter than `|`, so a union splits first. A union selects
    packages only when every operand does. A conjunction selects no more than
    its narrowest operand. Only `package(<name>)` and
    `package(=<name>)` with a plain name select packages: `not`, a difference,
    a glob, `test()` and `all()` select the workspace.
    """
    expression = without_outer_parentheses(expression)
    disjuncts = top_level_parts(expression, "|+", "or")
    if len(disjuncts) > 1:
        names: set[str] = set()
        for disjunct in disjuncts:
            selection = expression_selection(disjunct)
            if isinstance(selection, WholeWorkspace):
                return selection
            names |= selection.names
        return SelectedPackages(frozenset(names))
    conjuncts = top_level_parts(expression, "&", "and")
    if len(conjuncts) > 1:
        for conjunct in conjuncts:
            selection = expression_selection(conjunct)
            if isinstance(selection, SelectedPackages):
                return selection
        return WholeWorkspace()
    match = PACKAGE_ATOM.fullmatch(expression)
    return SelectedPackages(frozenset({match.group(1)})) if match is not None else WholeWorkspace()


def test_selection(words: tuple[str, ...]) -> Selection:
    """Name the packages that `cargo nextest run <words>` tests."""
    packages = option_values(words, PACKAGE_OPTIONS)
    if packages:
        if any(PACKAGE_NAME.fullmatch(name) is None for name in packages):
            return WholeWorkspace()
        return SelectedPackages(frozenset(packages))
    expressions = option_values(words, FILTER_OPTIONS)
    if not expressions:
        return WholeWorkspace()
    names: set[str] = set()
    for expression in expressions:
        selection = expression_selection(expression)
        if isinstance(selection, WholeWorkspace):
            return selection
        names |= selection.names
    return SelectedPackages(frozenset(names))


def needed(prebuilds: tuple[Prebuild, ...], selection: Selection) -> tuple[Prebuild, ...]:
    if isinstance(selection, WholeWorkspace):
        return prebuilds
    return tuple(prebuild for prebuild in prebuilds if prebuild.for_packages & selection.names)


def package_name(package_id: str) -> str:
    """Read the package name from a cargo package id, in the old or the spec form."""
    old_form = re.fullmatch(r"(\S+) \S+ \(.*\)", package_id)
    if old_form is not None:
        return old_form.group(1)
    source, _, fragment = package_id.partition("#")
    if "@" in fragment:
        return fragment.rpartition("@")[0]
    return source.partition("?")[0].rstrip("/").rpartition("/")[2]


def find_artifact(prebuild: Prebuild, messages: str) -> Artifact:
    """Read cargo's JSON messages for the package's one linked artifact: a cdylib or a bin."""
    linked: list[str] = []
    for line in messages.splitlines():
        try:
            message: object = json.loads(line)  # pyright: ignore[reportAny]
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue
        fields = cast(dict[str, object], message)
        package_id, target = fields.get("package_id"), fields.get("target")
        if (fields.get("reason") != "compiler-artifact" or not isinstance(package_id, str)
                or package_name(package_id) != prebuild.package or not isinstance(target, dict)):
            continue
        kinds = cast(dict[str, object], target).get("kind")
        if not isinstance(kinds, list) or not {"cdylib", "bin"} & set(cast(list[object], kinds)):
            continue
        executable, filenames = fields.get("executable"), fields.get("filenames")
        if isinstance(executable, str):
            linked.append(executable)
        elif isinstance(filenames, list):
            linked += [name for name in cast(list[object], filenames)
                       if isinstance(name, str) and not name.endswith(LIBRARY_SUFFIXES)]
    if len(linked) == 1:
        return ArtifactFound(linked[0])
    if not linked:
        return ArtifactMissing("made no cdylib or bin artifact")
    return ArtifactMissing("made more than one linked artifact: " + ", ".join(linked))


def host_platform() -> str:
    return "macos" if sys.platform == "darwin" else sys.platform


def build(prebuild: Prebuild, manifest: Path, words: tuple[str, ...]) -> int:
    """Build one artifact and set its variable; return cargo's status."""
    if host_platform() not in prebuild.platforms:
        print(f"prebuild: {prebuild.package} builds only on {', '.join(sorted(prebuild.platforms))}; "
              + f"not built on {host_platform()}", file=sys.stderr, flush=True)
        return 0
    command = ["cargo", "build", "--release", "--manifest-path", str(manifest),
               "-p", prebuild.package, "--target", prebuild.target]
    command += [f"--target-dir={value}" for value in option_values(words, ("--target-dir",))[-1:]]
    print("+ " + " ".join(command), file=sys.stderr, flush=True)
    result = subprocess.run([*command, "--message-format=json-render-diagnostics"],
                            stdout=subprocess.PIPE, text=True, check=False)
    if result.returncode != 0:
        print(f"prebuild: cargo build of {prebuild.package} failed (status {result.returncode}); the tests did not run",
              file=sys.stderr)
        return result.returncode
    artifact = find_artifact(prebuild, result.stdout)
    if isinstance(artifact, ArtifactMissing):
        print(f"prebuild: cargo build of {prebuild.package} {artifact.reason}; the tests did not run",
              file=sys.stderr)
        return 2
    os.environ[prebuild.env] = artifact.path
    print(f"prebuild: {prebuild.env}={artifact.path}", file=sys.stderr, flush=True)
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 4 or argv[0] != "--manifest" or argv[2] != "--":
        print("usage: prebuild.py --manifest <Cargo.toml> -- <command...>", file=sys.stderr)
        return 2
    manifest, command = Path(argv[1]), argv[3:]
    try:
        prebuilds = read_prebuilds(manifest)
    except (OSError, tomllib.TOMLDecodeError, DeclarationError) as error:
        print(f"prebuild: {manifest}: {error}", file=sys.stderr)
        return 2
    starts = [index for index in range(len(command)) if command[index:index + 3] == ["cargo", "nextest", "run"]]
    if starts:
        words = tuple(command[starts[0] + 3:])
        for prebuild in needed(prebuilds, test_selection(words)):
            status = build(prebuild, manifest, words)
            if status != 0:
                return status
    os.execvp(command[0], command)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
