#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Find test fixture copies of a config file that lack a key the tip adds to it.

A fixture text without the key makes the code under test fall back to the committed
value with a warning, or fail where no committed value exists, as on a box without git.
Each literal fixture text of a config file must hold each plain key that the tip adds to
that file. A per-name key (`kind.mac`, `value.natedev`) is not required, because a
fixture names only its own targets and hosts.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import ast
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import sys

from scripts.lib import conf
from scripts.production.affected_modules import GraphError, blob_texts, git_output, scope_nodes
from scripts.production.gate_modules import is_test_module
from scripts.style.git_process import parse_tree_listing


@dataclass(frozen=True)
class FixtureWrite:
    source: str
    line: int
    conf_name: str
    text: str | None
    """None when the written text is not a literal."""


@dataclass(frozen=True)
class MissingKey:
    write: FixtureWrite
    key: str


@dataclass(frozen=True)
class KeyReport:
    new_keys: dict[str, set[str]]
    missing: list[MissingKey]
    not_checked: list[FixtureWrite]


def new_keys(base: dict[str, str], tip: dict[str, str]) -> dict[str, set[str]]:
    """Map each config file name to the plain keys the tip adds."""
    added: dict[str, set[str]] = {}
    for name, text in tip.items():
        before = set(conf.settings(base.get(name, "")))
        keys = {key for key in conf.settings(text) if key not in before and "." not in key}
        if keys:
            added[name] = keys
    return added


Scope = ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef


@dataclass(frozen=True)
class ScopeBindings:
    conf: dict[str, set[str]]
    """The config file names that each bound name or attribute text receives."""
    other: set[str]
    """Names bound to a value without a config file name, such as a loop variable."""


def conf_names(node: ast.AST) -> set[str]:
    return {PurePosixPath(child.value).name for child in ast.walk(node)
            if isinstance(child, ast.Constant) and isinstance(child.value, str) and child.value.endswith(".conf")}


def own_nodes(scope: Scope) -> Iterator[ast.AST]:
    for child in ast.iter_child_nodes(scope):
        yield from scope_nodes(child)


def scope_bindings(scope: Scope) -> ScopeBindings:
    bindings = ScopeBindings({}, set())

    def bind(target: ast.AST, value: ast.AST | None) -> None:
        names: set[str] = conf_names(value) if value is not None else set()
        for part in ast.walk(target):
            if isinstance(part, ast.Name | ast.Attribute):
                text = ast.unparse(part)
                if names and part is target:
                    bindings.conf.setdefault(text, set()).update(names)
                else:
                    bindings.other.add(text)

    if isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
        for argument in (*scope.args.posonlyargs, *scope.args.args, *scope.args.kwonlyargs):
            bindings.other.add(argument.arg)
    for node in own_nodes(scope):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                bind(target, node.value)
        elif isinstance(node, ast.AnnAssign | ast.AugAssign | ast.NamedExpr):
            bind(node.target, node.value)
        elif isinstance(node, ast.For | ast.AsyncFor | ast.comprehension):
            bind(node.target, None)
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            bind(node.optional_vars, None)
    return bindings


class FixtureSource:
    """Resolve the config file names and texts of fixture writes in one source."""

    def __init__(self, tree: ast.Module) -> None:
        self.parents: dict[ast.AST, ast.AST] = {child: node for node in ast.walk(tree)
                                                for child in ast.iter_child_nodes(node)}
        self.bindings: dict[ast.AST, ScopeBindings] = {}
        self.texts: dict[str, str] = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                if (text := self.literal_text(node.value)) is not None:
                    self.texts[node.targets[0].id] = text

    def scopes(self, node: ast.AST) -> Iterator[Scope]:
        """Yield the scopes that hold a node, the innermost first."""
        current = self.parents.get(node)
        while current is not None:
            if isinstance(current, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                yield current
            current = self.parents.get(current)

    def bound(self, scope: Scope) -> ScopeBindings:
        if scope not in self.bindings:
            self.bindings[scope] = scope_bindings(scope)
        return self.bindings[scope]

    def assigned(self, part: ast.Name | ast.Attribute) -> set[str]:
        """Return the config file names of a name, from its nearest binding scope.

        A name that the scope also binds to another value is not resolved. An attribute
        of self resolves through each method of its class.
        """
        text = ast.unparse(part)
        for scope in self.scopes(part):
            if isinstance(scope, ast.ClassDef):
                if not text.startswith("self."):
                    continue
                methods = [self.bound(node) for node in scope.body
                           if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)]
                found = [bindings for bindings in methods if text in bindings.conf or text in bindings.other]
                if found:
                    return set() if any(text in bindings.other for bindings in found) else {
                        name for bindings in found for name in bindings.conf[text]}
                continue
            bindings = self.bound(scope)
            if text in bindings.other:
                return set()
            if text in bindings.conf:
                return bindings.conf[text]
        return set()

    def conf_files(self, path: ast.expr) -> set[str]:
        return conf_names(path) | {name for part in ast.walk(path) if isinstance(part, ast.Name | ast.Attribute)
                                   for name in self.assigned(part)}

    def literal_text(self, node: ast.expr) -> str | None:
        """Return a literal text; a value inside an f-string reads as 0."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            return "".join(str(part.value) if isinstance(part, ast.Constant) else "0" for part in node.values)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = self.literal_text(node.left), self.literal_text(node.right)
            return left + right if left is not None and right is not None else None
        if isinstance(node, ast.Name):
            return self.texts.get(node.id)
        return None


def written(call: ast.Call) -> tuple[ast.expr, ast.expr] | None:
    """Return the path and text of a `write_text` or `write_text_atomic` call.

    `write_config_fixture` starts from the repository template, so its result holds
    every key and is not checked.
    """
    function = call.func
    if isinstance(function, ast.Attribute) and function.attr == "write_text" and call.args:
        return function.value, call.args[0]
    atomic = (isinstance(function, ast.Attribute) and function.attr == "write_text_atomic"
              or isinstance(function, ast.Name) and function.id == "write_text_atomic")
    return (call.args[0], call.args[1]) if atomic and len(call.args) >= 2 else None


def fixture_writes(source: str, text: str, conf_files: set[str]) -> Iterator[FixtureWrite]:
    tree = ast.parse(text, source)
    fixture = FixtureSource(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and (parts := written(node)) is not None:
            path, content = parts
            for name in sorted(fixture.conf_files(path) & conf_files):
                yield FixtureWrite(source, node.lineno, name, fixture.literal_text(content))


def is_fixture_source(path: str) -> bool:
    name = PurePosixPath(path).name
    return path.startswith("scripts/") and (
        is_test_module(path) or name.endswith(("_support.py", "_fixture.py", "_fixtures.py")))


def regular_files(repo: Path, revision: str) -> dict[str, str]:
    """Map each regular file of a revision to its blob; a symbolic link is left out."""
    entries = parse_tree_listing(git_output(repo, "ls-tree", "-rz", revision))
    return {path: entry.object for path, entry in entries.items() if entry.kind == "blob" and entry.mode != "120000"}


def check(repo: Path, base: str, tip: str) -> KeyReport:
    base_files, tip_files = regular_files(repo, base), regular_files(repo, tip)

    def confs(files: dict[str, str]) -> dict[str, str]:
        return {path: blob for path, blob in files.items() if path.startswith("config/") and path.endswith(".conf")}

    base_confs, tip_confs = confs(base_files), confs(tip_files)
    texts = blob_texts(repo, set(base_confs.values()) | set(tip_confs.values()))
    added = new_keys({PurePosixPath(path).name: texts[blob] for path, blob in base_confs.items()},
                     {PurePosixPath(path).name: texts[blob] for path, blob in tip_confs.items()})
    missing: list[MissingKey] = []
    not_checked: list[FixtureWrite] = []
    sources = {path: blob for path, blob in tip_files.items() if is_fixture_source(path)} if added else {}
    texts = blob_texts(repo, set(sources.values())) if sources else {}
    for source, blob in sorted(sources.items()):
        for write in sorted(fixture_writes(source, texts[blob], set(added)), key=lambda write: write.line):
            if write.text is None:
                not_checked.append(write)
            else:
                held = set(conf.settings(write.text))
                missing.extend(MissingKey(write, key) for key in sorted(added[write.conf_name] - held))
    return KeyReport(added, missing, not_checked)


def report(result: KeyReport) -> int:
    for name, keys in sorted(result.new_keys.items()):
        print(f"new keys in config/{name}: {', '.join(sorted(keys))}")
    for gap in result.missing:
        print(f"{gap.write.source}:{gap.write.line}: the config/{gap.write.conf_name} fixture lacks {gap.key}")
    print(f"fixture config keys: {len(result.missing)} missing, "
          + f"{len(result.not_checked)} fixtures not checked because their text is not a literal")
    return 1 if result.missing else 0


class Arguments(argparse.Namespace):
    repo: Path = Path(".")
    base: str = ""
    tip: str = ""


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    _ = parser.add_argument("--repo", type=Path, default=Path("."))
    _ = parser.add_argument("--base", required=True, help="the revision the tip joins, such as origin/main")
    _ = parser.add_argument("--tip", required=True, help="the joined commit that the row proof tests")
    arguments = parser.parse_args(argv, namespace=Arguments())
    try:
        for revision in (arguments.base, arguments.tip):
            _ = git_output(arguments.repo, "rev-parse", "--verify", f"{revision}^{{commit}}")
        return report(check(arguments.repo, arguments.base, arguments.tip))
    except (GraphError, SyntaxError, UnicodeDecodeError) as error:
        print(f"fixture_conf_keys: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
