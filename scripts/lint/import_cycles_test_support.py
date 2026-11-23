"""Find import cycles among the .py files in scripts/.

basedpyright reportImportCycles counts every import statement: a lazy import inside a
function and an import under `if TYPE_CHECKING:` count too. A string given to importlib
does not count. This module counts the same statements, and _Resolver copies the
basedpyright search rules that this repository uses.

A fast pass reads the bytes of each file with compiled regexes. It is built to find
every real import, and it also finds import text inside a string. When the fast graph
has no cycle, the answer is empty and no file is parsed. When it has cycles, a confirm
pass parses only the member files with ast and keeps the real import statements.
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import NamedTuple, NotRequired, TypedDict, cast

SCAN_ROOT = "scripts"

# The fast pass finds each "import" keyword with a literal search, which is the
# cheapest scan. Then it reads the text before the keyword on the logical line: an
# empty text, or a text that ends with ";" or ":", starts "import a.b as c, d";
# a text that ends with "from a.b" starts "from a.b import x". The names follow the
# keyword: on one line with backslash continuations, or in parentheses over lines.
# A dotted name can have spaces around its dots: "import a . b".
_KEYWORD = re.compile(rb"import(?=[ \t\f(\\])")
_FROM = re.compile(rb"(?:^|[;:])[ \t\f]*from\b[ \t\f]*([\w.\x80-\xff][\w.\x80-\xff \t\f]*)\Z")
_NAMES = re.compile(rb"(?:[ \t\f]|\\\r?\n)+((?:[^\n;#\\]|\\\r?\n)+)")
_TARGETS = re.compile(rb"(?:[ \t\f]|\\\r?\n)*(?:\(((?:[^)#]|#[^\n]*)*)\)|((?:[^\n;#\\(]|\\\r?\n)+))")
_CONTINUATION = re.compile(rb"\\\r?\n|#[^\n]*")
_DOT = re.compile(rb"[ \t\f]*\.[ \t\f]*")
_WORD = frozenset(b"_0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")


class _Environment(TypedDict):
    root: str
    extraPaths: NotRequired[list[str]]


class _Config(TypedDict):
    extraPaths: NotRequired[list[str]]
    executionEnvironments: NotRequired[list[_Environment]]


# One import: the module text (leading dots for a relative import); for
# "from ... import", the imported names; and True for a plain "import a.b.c" with
# no "as". Only a plain import counts the __init__ file of each package on the path.
type _Import = tuple[str, tuple[str, ...] | None, bool]
type _Graph = dict[str, set[str]]


def cycles(repo: Path) -> list[list[str]]:
    """Return each import cycle in scripts/ as sorted repository-relative paths."""
    resolver = _Resolver(repo)
    fast = _components(_fast_graph(resolver))
    if not fast:
        return []
    members = {member for component in fast for member in component}
    confirmed: _Graph = {}
    for source in sorted(members):
        tree = ast.parse((repo / source).read_bytes(), filename=source)
        targets = resolver.targets(source, _ast_imports(tree))
        confirmed[source] = {target for target in targets if target in members}
    return _components(confirmed)


def _fast_graph(resolver: _Resolver) -> _Graph:
    """Build the import graph of scripts/ from the regex pass."""
    graph: _Graph = {}
    for source in resolver.scan():
        with open(resolver.repo / source, "rb") as handle:
            text = handle.read()
        graph[source] = resolver.targets(source, _regex_imports(text))
    return graph


def _regex_imports(text: bytes) -> Iterator[_Import]:
    """Yield each import statement the fast pass finds, also import text inside a string.

    Each import is marked plain, so that the fast graph stays a superset of the real graph.
    """
    for keyword in _KEYWORD.finditer(text):
        position = keyword.start()
        if position and text[position - 1] in _WORD:
            continue
        line = text.rfind(b"\n", 0, position) + 1
        while text.endswith(b"\\\n", 0, line) or text.endswith(b"\\\r\n", 0, line):
            previous = text.rfind(b"\n", 0, line - 2) + 1
            if b"#" in text[previous:line]:
                break  # A backslash at the end of a comment is not a continuation.
            line = previous
        before = text[line:position]
        if b"\\" in before:
            before = _CONTINUATION.sub(b" ", before)
        lead = before.rstrip(b" \t\f")
        if not lead or lead[-1] in b";:":
            names = _NAMES.match(text, keyword.end())
            if names is not None:
                for item in _items(names.group(1)):
                    yield item, None, True
            continue
        module = _FROM.search(lead)
        targets = _TARGETS.match(text, keyword.end())
        if module is not None and targets is not None:
            listed = cast(bytes, targets.group(1) if targets.group(1) is not None else targets.group(2))
            module_name = b"".join(module.group(1).split()).decode("utf-8", "replace")
            yield module_name, tuple(_items(listed)), True


def _items(text: bytes) -> Iterator[str]:
    """Yield the first word of each comma-separated item: "a.b as c, d" gives a.b and d."""
    for item in _DOT.sub(b".", _CONTINUATION.sub(b" ", text)).split(b","):
        words = item.split()
        if words:
            yield words[0].decode("utf-8", "replace")


def _ast_imports(tree: ast.Module) -> Iterator[_Import]:
    """Yield each Import and ImportFrom statement. Walk statement bodies only."""
    pending: list[ast.AST] = list(tree.body)
    while pending:
        node = pending.pop()
        match node:
            case ast.Import():
                for alias in node.names:
                    yield alias.name, None, alias.asname is None
            case ast.ImportFrom():
                module = "." * node.level + (node.module or "")
                yield module, tuple(alias.name for alias in node.names), False
            case _:
                pending.extend(_child_statements(node))


def _child_statements(node: ast.AST) -> list[ast.AST]:
    """Return the statements, handlers and match cases directly inside a statement."""
    match node:
        case (ast.FunctionDef() | ast.AsyncFunctionDef() | ast.ClassDef() | ast.With()
              | ast.AsyncWith() | ast.ExceptHandler() | ast.match_case()):
            return list(node.body)
        case ast.If() | ast.For() | ast.AsyncFor() | ast.While():
            return [*node.body, *node.orelse]
        case ast.Try() | ast.TryStar():
            return [*node.body, *node.handlers, *node.orelse, *node.finalbody]
        case ast.Match():
            return list(node.cases)
        case _:
            return []


class _Hit(NamedTuple):
    """A resolved module name."""

    packages: tuple[str, ...]  # The __init__ file of each package on the path before the module.
    file: str | None  # The module file: a package __init__, a stub or a .py file. None for a namespace folder.
    directories: tuple[str, ...]  # The package folders. A namespace name merges its folders in all search roots.


class _Resolver:
    """Resolve import names to repository files with the basedpyright search rules.

    A file uses the first execution environment whose root holds it. Its search roots
    are that root, then the environment extraPaths (else the top-level extraPaths). A
    file that no environment holds uses the repository root and the top-level
    extraPaths. When the search roots do not resolve an absolute import, the search
    walks up from the folder of the file to the environment root. A one-part stdlib name
    resolves outside the repository. A longer stdlib name is searched in the repository,
    because basedpyright uses a local package when typeshed does not have the full name.
    Gap: when a local package shadows a stdlib package and typeshed has the full name,
    this resolver uses the local file. In a folder, a package comes before a stub, and a
    stub comes before a .py file.
    """

    def __init__(self, repo: Path) -> None:
        self.repo: Path = repo
        config = cast(_Config, json.loads((repo / "pyrightconfig.json").read_text(encoding="utf-8")))
        default_extra = [_relative(path) for path in config.get("extraPaths", [])]
        self._default: tuple[str, tuple[str, ...]] = ("", tuple(dict.fromkeys(["", *default_extra])))
        self._environments: list[tuple[str, tuple[str, ...]]] = []
        for environment in config.get("executionEnvironments", []):
            root = _relative(environment["root"])
            extra = [_relative(path) for path in environment.get("extraPaths", default_extra)]
            self._environments.append((root, tuple(dict.fromkeys([root, *extra]))))
        self._listings: dict[str, frozenset[str]] = {}
        self._packages: dict[str, tuple[str, tuple[str, ...]]] = {}
        self._in_base: dict[tuple[str, str], _Hit | None] = {}
        self._absolute: dict[tuple[tuple[str, ...], str], _Hit | None] = {}
        self._walked: dict[tuple[str, str], _Hit | None] = {}

    def scan(self) -> list[str]:
        """Return each .py file in scripts/. Skip __pycache__ and hidden folders."""
        files: list[str] = []
        for directory, folders, names in os.walk(self.repo / SCAN_ROOT):
            relative = os.path.relpath(directory, self.repo)
            self._listings[relative] = frozenset([*folders, *names])
            folders[:] = [name for name in folders if name != "__pycache__" and not name.startswith(".")]
            files.extend(f"{relative}/{name}" for name in names if name.endswith(".py"))
        return files

    def targets(self, source: str, imports: Iterable[_Import]) -> set[str]:
        """Return the repository files that the imports of source resolve to."""
        package = source.rpartition("/")[0]
        found: set[str] = set()
        for module, names, plain in imports:
            if module.startswith("."):
                stripped = module.lstrip(".")
                hit = self._resolve_in(_parent(package, len(module) - len(stripped) - 1), stripped)
            else:
                hit = self._resolve_absolute(package, module)
            if hit is None:
                continue
            if plain:
                found.update(hit.packages)
            if hit.file is not None:
                found.add(hit.file)
            if names is None or not hit.directories:
                continue
            for name in names:
                if name == "*":
                    # basedpyright takes a star import from the first namespace folder only.
                    found.update(self._package_modules(hit.directories[:1]))
                    continue
                child = self._child(hit.directories, name)
                if child is not None:
                    found.add(child)
        found.discard(source)
        return found

    def _environment(self, package: str) -> tuple[str, tuple[str, ...]]:
        """Return the root and search roots of the first environment that holds the package."""
        environment = self._packages.get(package)
        if environment is None:
            environment = self._default
            for root, bases in self._environments:
                if not root or package == root or package.startswith(root + "/"):
                    environment = (root, bases)
                    break
            self._packages[package] = environment
        return environment

    def _resolve_absolute(self, package: str, module: str) -> _Hit | None:
        """Resolve an absolute name in the search roots, else walk up from the package."""
        if module in sys.stdlib_module_names:
            return None
        root, bases = self._environment(package)
        key = (bases, module)
        if key in self._absolute:
            hit = self._absolute[key]
        else:
            hit = self._absolute[key] = self._first(bases, module)
        if hit is not None:
            return hit
        walk = (package, module)
        if walk not in self._walked:
            self._walked[walk] = self._first(_folders_up(package, root), module)
        return self._walked[walk]

    def _first(self, bases: Iterable[str], module: str) -> _Hit | None:
        """Return the module in the first base that has one, else merge the namespace folders."""
        namespace: _Hit | None = None
        folders: list[str] = []
        for base in bases:
            hit = self._resolve_in(base, module)
            if hit is None:
                continue
            if hit.file is not None:
                return hit
            if namespace is None:
                namespace = hit
            folders.extend(hit.directories)
        if namespace is None:
            return None
        return namespace._replace(directories=tuple(dict.fromkeys(folders)))

    def _resolve_in(self, base: str, module: str) -> _Hit | None:
        """Resolve a dotted name in one base folder. An empty name is the base folder."""
        key = (base, module)
        if key not in self._in_base:
            self._in_base[key] = self._lookup(base, module.split(".") if module else [])
        return self._in_base[key]

    def _lookup(self, base: str, parts: list[str]) -> _Hit | None:
        """Resolve the name parts in one base folder. Return None when the name is not there."""
        packages: list[str] = []
        directory = base
        for part in parts[:-1]:
            if part not in self._names(directory):
                return None
            directory = _join(directory, part)
            init = self._init(directory)
            if init is not None:
                packages.append(init)
        if not parts:
            init = self._init(directory)
            if init is None and not self._names(directory):
                return None
            return _Hit((), init, (directory,))
        last = parts[-1]
        names = self._names(directory)
        package = _join(directory, last)
        if last in names:
            init = self._init(package)
            if init is not None:
                return _Hit(tuple(packages), init, (package,))
        for suffix in (".pyi", ".py"):
            if last + suffix in names:
                return _Hit(tuple(packages), _join(directory, last + suffix), ())
        if last in names and self._names(package):
            return _Hit(tuple(packages), None, (package,))
        return None

    def _child(self, directories: tuple[str, ...], name: str) -> str | None:
        """Return the file of the submodule name in the first package folder that has it."""
        for directory in directories:
            names = self._names(directory)
            if name in names:
                init = self._init(_join(directory, name))
                if init is not None:
                    return init
            for suffix in (".pyi", ".py"):
                if name + suffix in names:
                    return _join(directory, name + suffix)
        return None

    def _package_modules(self, directories: tuple[str, ...]) -> set[str]:
        """Return the file of each submodule in the package folders, for "import *"."""
        stems = {name.rpartition(".")[0] if name.endswith((".py", ".pyi")) else name
                 for directory in directories for name in self._names(directory)}
        stems.discard("__init__")
        return {child for stem in stems if (child := self._child(directories, stem)) is not None}

    def _init(self, directory: str) -> str | None:
        """Return the __init__ stub or file of a folder, else None."""
        names = self._names(directory)
        for name in ("__init__.pyi", "__init__.py"):
            if name in names:
                return _join(directory, name)
        return None

    def _names(self, directory: str) -> frozenset[str]:
        """Return the entry names of a repository folder, read once."""
        names = self._listings.get(directory)
        if names is None:
            try:
                names = frozenset(os.listdir(str(self.repo / directory)))
            except OSError:
                names = frozenset[str]()
            self._listings[directory] = names
        return names


def _components(graph: _Graph) -> list[list[str]]:
    """Return each strongly connected component of more than one node (iterative Tarjan)."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    found: list[list[str]] = []
    for start in graph:
        if start in index:
            continue
        index[start] = low[start] = len(index)
        stack.append(start)
        on_stack.add(start)
        work: list[tuple[str, Iterator[str]]] = [(start, iter(graph[start]))]
        while work:
            node, children = work[-1]
            for child in children:
                if child not in index:
                    index[child] = low[child] = len(index)
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(graph.get(child, ()))))
                    break
                if child in on_stack:
                    low[node] = min(low[node], index[child])
            else:
                _ = work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[node])
                if low[node] == index[node]:
                    component: list[str] = []
                    while True:
                        member = stack.pop()
                        on_stack.discard(member)
                        component.append(member)
                        if member == node:
                            break
                    if len(component) > 1:
                        found.append(sorted(component))
    return sorted(found)


def _relative(path: str) -> str:
    """Normalize a config path to a repository-relative folder; "." becomes ""."""
    normal = os.path.normpath(path)
    return "" if normal == "." else normal


def _join(directory: str, name: str) -> str:
    return f"{directory}/{name}" if directory else name


def _parent(package: str, levels: int) -> str:
    """Return the folder that a relative import with levels + 1 dots names."""
    for _ in range(levels):
        package = package.rpartition("/")[0]
    return package


def _folders_up(package: str, root: str) -> Iterator[str]:
    """Yield the package folder, then each parent folder up to the environment root."""
    folder = package
    yield folder
    while folder != root and folder:
        folder = folder.rpartition("/")[0]
        yield folder
