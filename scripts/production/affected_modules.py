"""Select tests from imports, recorded reads, folder scans, and script calls.

No source is imported or evaluated. Callers own certification and execution.
"""
from __future__ import annotations

import ast
from collections import defaultdict
from collections.abc import Generator, Iterator, Sequence
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass, field
from enum import Enum
import fcntl
from functools import cache
from hashlib import sha256
from importlib.metadata import packages_distributions
from io import BytesIO
from itertools import chain
from pathlib import Path, PurePosixPath
import json
import os
import posixpath
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from time import monotonic
from typing import cast

from scripts.lib import conf
from scripts.mac_test.eligibility import MAC_LINUX_TARGET
from scripts.test_times.gate_record import (
    ListedDirectory, ModuleReadRecord, NoConfigReadRecord, NoContentReadRecord,
    NoFolderScanRecord, RecordedConfigReads, RecordedContentReads, RecordedFolderScans,
    RecursiveDirectory, WholeRepository, module_read_record,
)
from scripts.production.gate_modules import is_test_module
from scripts.style.git_process import GitBlob, parse_status_z, parse_tree_listing, read_batch_blob


@dataclass(frozen=True)
class CertifiedBase:
    commit: str
    at: str


@dataclass(frozen=True)
class NoCertifiedBase:
    pass


class FactCacheLock(Enum):
    NOT_USED = "not used"
    UNAVAILABLE = "unavailable"
    BUSY = "busy, no snapshot"
    BUSY_SNAPSHOT = "busy, read old snapshot"
    TAKEN = "taken"
    PUBLISHED = "taken, published"
    PUBLISH_FAILED = "taken, publish failed"


@dataclass
class SelectionRun:
    cache_hits: int = 0
    cache_misses: int = 0
    extracted_files: int = 0
    cache_read_s: float = 0.0
    extraction_s: float = 0.0
    graph_s: float = 0.0
    total_s: float = 0.0
    lock: FactCacheLock = FactCacheLock.NOT_USED

    @property
    def other_s(self) -> float:
        return self.total_s - self.cache_read_s - self.extraction_s - self.graph_s

    @contextmanager
    def graph_build(self) -> Generator[None]:
        started = monotonic()
        extraction_before = self.extraction_s
        try:
            yield
        finally:
            self.graph_s += (monotonic() - started) - (self.extraction_s - extraction_before)


@dataclass(frozen=True)
class SelectionNotRun:
    pass


@dataclass(frozen=True)
class AffectedSelection:
    base: CertifiedBase
    changed_files: tuple[str, ...]
    modules: tuple[Path, ...]
    run: SelectionRun | SelectionNotRun = field(default_factory=SelectionNotRun, compare=False)


@dataclass(frozen=True)
class FullSuite:
    reason: str
    run: SelectionRun | SelectionNotRun = field(default_factory=SelectionNotRun, compare=False)


class GraphError(Exception):
    """Git or source cannot establish a safe dependency graph."""


def git_output(repo: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *arguments],
                            capture_output=True, text=True, errors="replace", check=False)
    if result.returncode:
        raise GraphError(result.stderr.strip() or f"git {' '.join(arguments)} failed")
    return result.stdout


# Bump the extractor version whenever source-fact semantics change.
CACHE_FORMAT_VERSION = 4
EXTRACTOR_VERSION = 11
FACT_CACHE_NAME = f"affected-module-facts-{CACHE_FORMAT_VERSION}-{EXTRACTOR_VERSION}.sqlite"
PATH_TOKEN = re.compile(r"[A-Za-z0-9_.\-/]+")


@dataclass(frozen=True)
class ValidPython:
    pass


@dataclass(frozen=True)
class InvalidPython:
    message: str


@dataclass(frozen=True)
class SourceFileRoot:
    pass


@dataclass(frozen=True)
class AbsolutePathRoot:
    path: str


@dataclass(frozen=True)
class ParameterRoot:
    name: str


@dataclass(frozen=True)
class ImportedRoot:
    target: str


@dataclass(frozen=True)
class UnrootedPath:
    pass


@dataclass(frozen=True)
class OutsideRepository:
    pass


@dataclass(frozen=True)
class ScanPath:
    root: SourceFileRoot | AbsolutePathRoot | ParameterRoot | ImportedRoot
    parts: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScanPattern:
    directory: ScanPath | UnrootedPath
    pattern: str
    recursive: bool


@dataclass(frozen=True)
class ScanCall:
    target: str
    arguments: tuple[ScanPath | UnrootedPath, ...]
    keywords: tuple[tuple[str, ScanPath | UnrootedPath], ...]


type SourceBindings = tuple[tuple[str, ScanPath | UnrootedPath], ...]


@dataclass(frozen=True)
class ScanScope:
    name: str
    parameters: tuple[str, ...]
    bindings: SourceBindings
    scans: tuple[ScanPattern, ...]
    calls: tuple[ScanCall, ...]


@dataclass(frozen=True, order=True)
class RepositoryScan:
    directory: str
    pattern: str
    recursive: bool


@dataclass(frozen=True)
class SourceFacts:
    # Relative imports retain their level until resolved from the current path.
    imports: tuple[tuple[int, str, tuple[str, ...]], ...]
    tokens: frozenset[str]
    config_reads: frozenset[str]
    syntax: ValidPython | InvalidPython = ValidPython()
    scan_scopes: tuple[ScanScope, ...] = ()


@dataclass(frozen=True)
class SourceGraph:
    imports: dict[str, set[str]]
    tokens: dict[str, frozenset[str]]
    config_reads: dict[str, frozenset[str]]
    repository_scans: dict[str, frozenset[RepositoryScan]]


def path_tokens(text: str) -> frozenset[str]:
    return frozenset(match.group().removeprefix("./") for match in PATH_TOKEN.finditer(text))


def scope_nodes(node: ast.AST) -> Iterator[ast.AST]:
    """Visit one lexical scope without entering a nested scope."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
        return
    yield node
    for child in ast.iter_child_nodes(node):
        yield from scope_nodes(child)


def scan_path(node: ast.AST, bindings: dict[str, ScanPath | UnrootedPath],
              aliases: dict[str, str]) -> ScanPath | UnrootedPath:
    """Retain a source path without reading or executing source."""
    if isinstance(node, (ast.Name, ast.Attribute)):
        name = ast.unparse(node)
        if name in bindings:
            return bindings[name]
    if isinstance(node, ast.Name) and node.id == "__file__":
        return ScanPath(SourceFileRoot())
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        if isinstance(node.right, ast.Constant) and isinstance(node.right.value, str) and PurePosixPath(node.right.value).is_absolute():
            return ScanPath(AbsolutePathRoot(node.right.value))
        left = scan_path(node.left, bindings, aliases)
        if isinstance(left, ScanPath) and isinstance(node.right, ast.Constant) and isinstance(node.right.value, str):
            suffix = PurePosixPath(node.right.value)
            return ScanPath(left.root, (*left.parts, *suffix.parts))
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        parent = scan_path(node.value, bindings, aliases)
        if isinstance(parent, ScanPath):
            return ScanPath(parent.root, (*parent.parts, ".."))
    if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) and node.value.attr == "parents"
            and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, int) and node.slice.value >= 0):
        parent = scan_path(node.value.value, bindings, aliases)
        if isinstance(parent, ScanPath):
            return ScanPath(parent.root, parent.parts + ("..",) * (node.slice.value + 1))
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute) and node.func.attr in {"resolve", "absolute", "expanduser"}:
            return scan_path(node.func.value, bindings, aliases)
        if ast.unparse(node.func).rsplit(".", 1)[-1] in {"Path", "PurePath", "PurePosixPath"} and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str) and PurePosixPath(first.value).is_absolute():
                result = ScanPath(AbsolutePathRoot(first.value))
            else:
                result = scan_path(first, bindings, aliases)
            for argument in node.args[1:]:
                if not isinstance(result, ScanPath) or not isinstance(argument, ast.Constant) or not isinstance(argument.value, str):
                    return UnrootedPath()
                suffix = PurePosixPath(argument.value)
                if suffix.is_absolute():
                    result = ScanPath(AbsolutePathRoot(argument.value))
                else:
                    result = ScanPath(result.root, (*result.parts, *suffix.parts))
            return result
    if isinstance(node, (ast.Name, ast.Attribute)):
        name = ast.unparse(node)
        for alias, target in aliases.items():
            if name == alias or name.startswith(alias + "."):
                return ScanPath(ImportedRoot(target + name.removeprefix(alias)))
    return UnrootedPath()


type ScanNode = ast.Import | ast.ImportFrom | ast.Assign | ast.AnnAssign | ast.Call
type ScopeDefinition = ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef


def source_scan_scopes(definitions: dict[str, tuple[ScopeDefinition, list[ScanNode]]],
                       ls_files_calls: set[ast.Call]) -> tuple[ScanScope, ...]:
    """Resolve each scope after the walk records all definitions."""
    scopes: list[ScanScope] = []
    environments: dict[str, tuple[dict[str, ScanPath | UnrootedPath], dict[str, str]]] = {}
    for name, (definition, nodes) in definitions.items():
        parent = name.rpartition(".")[0]
        inherited, imported = environments.get(parent, ({}, {}))
        bindings = dict(inherited)
        aliases = dict(imported)
        parameters: tuple[str, ...] = ()
        if isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef)):
            parameters = tuple(argument.arg for argument in (*definition.args.posonlyargs, *definition.args.args,
                                                             *definition.args.kwonlyargs))
            if parameters and parameters[0] in {"self", "cls"}:
                parameters = parameters[1:]
            bindings.update((parameter, ScanPath(ParameterRoot(parameter))) for parameter in parameters)
        scans: list[ScanPattern] = []
        calls: list[ScanCall] = []
        for node in nodes:
            if isinstance(node, ast.Import):
                aliases.update((alias.asname or alias.name, alias.name) for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                module = "." * node.level + (node.module or "")
                aliases.update((alias.asname or alias.name, module + ("." if node.module else "") + alias.name)
                               for alias in node.names)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                value = scan_path(node.value, bindings, aliases)
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                bindings.update((ast.unparse(target), value) for target in targets
                                if isinstance(target, (ast.Name, ast.Attribute)))
            elif isinstance(node, ast.Call):
                target = ast.unparse(node.func)
                if target.startswith(("self.", "cls.")):
                    target = parent + "." + target.split(".", 1)[1]
                for alias, imported_name in aliases.items():
                    if target == alias or target.startswith(alias + "."):
                        target = imported_name + target.removeprefix(alias)
                        break
                for prefix in (name, parent):
                    if f"{prefix}.{target}".lstrip(".") in definitions:
                        target = f"{prefix}.{target}".lstrip(".")
                        break
                arguments = tuple(scan_path(argument, bindings, aliases) for argument in node.args)
                keywords = tuple((keyword.arg, scan_path(keyword.value, bindings, aliases))
                                 for keyword in node.keywords if keyword.arg is not None)
                if target in definitions or any(isinstance(value, ScanPath) for value in (*arguments, *(item[1] for item in keywords))):
                    calls.append(ScanCall(target, arguments, keywords))
                if isinstance(node.func, ast.Attribute) and node.func.attr in {"glob", "rglob"}:
                    directory = scan_path(node.func.value, bindings, aliases)
                    pattern = node.args[0] if node.args else ast.Constant("*")
                    if isinstance(pattern, ast.Constant) and isinstance(pattern.value, str):
                        scans.append(ScanPattern(directory, pattern.value, node.func.attr == "rglob"))
                    else:
                        scans.append(ScanPattern(directory, "*", True))
                elif target.endswith(".walk") and arguments:
                    scans.append(ScanPattern(arguments[0], "*", True))
                elif node in ls_files_calls:
                    directories = [value for key, value in keywords if key == "cwd"] or list(arguments)
                    if directories:
                        scans.append(ScanPattern(directories[0], "*", True))
        environments[name] = bindings, aliases
        scopes.append(ScanScope(name, parameters, tuple(bindings.items()), tuple(scans), tuple(calls)))
    return tuple(scopes)


class _SourceFactWalk:
    def __init__(self, tree: ast.Module, config_reads: set[str]) -> None:
        self.definitions: dict[str, tuple[ScopeDefinition, list[ScanNode]]] = {"": (tree, [])}
        self.imports_by_depth: dict[int, list[ast.Import | ast.ImportFrom]] = defaultdict(list)
        self.config_calls: list[tuple[ast.expr, str]] = []
        self.ls_files_calls: set[ast.Call] = set()
        self.walk(tree, config_reads)

    def walk(self, tree: ast.Module, config_reads: set[str]) -> None:
        stack: list[tuple[ast.AST, int, str, list[ScanNode] | None, bool] | None] = [
            (tree, 0, "", self.definitions[""][1], False)
        ]
        active_calls: list[ast.Call] = []
        while stack:
            entry = stack.pop()
            if entry is None:
                _ = active_calls.pop()
                continue
            node, depth, name, scope, in_copy_source = entry
            child_depth = depth + 1
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{name}.{node.name}".lstrip(".")
                body: list[ScanNode] = []
                self.definitions[name] = node, body
                # Scans use only the body. Imports and definitions can also occur in other fields.
                fields = list(cast(Iterator[tuple[str, object]], ast.iter_fields(node)))
                for field, value in reversed(fields):
                    children = cast(list[object], value) if isinstance(value, list) else [value]
                    child_scope = body if field == "body" else None
                    for child in reversed(children):
                        if isinstance(child, ast.AST):
                            stack.append((child, child_depth, name, child_scope, in_copy_source))
                continue

            if isinstance(node, ast.Lambda):
                scope = None
            copy_source: ast.expr | None = None
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                self.imports_by_depth[depth].append(node)
                if scope is not None:
                    scope.append(node)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                if scope is not None:
                    scope.append(node)
            elif isinstance(node, ast.Call):
                if scope is not None:
                    scope.append(node)
                active_calls.append(node)
                # Pop the active call after its children.
                stack.append(None)
                if node.args:
                    first = node.args[0]
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        self.config_calls.append((node.func, first.value))
                    if isinstance(node.func, ast.Attribute) and node.func.attr == "copytree":
                        copy_source = first
            elif isinstance(node, ast.Constant):
                if node.value == "ls-files":
                    self.ls_files_calls.update(active_calls)
                if in_copy_source and node.value == "config":
                    config_reads.add("*")
                continue
            elif isinstance(node, ast.JoinedStr) and any(
                    isinstance(part, ast.Constant) and isinstance(part.value, str) and part.value.endswith(".conf")
                    for part in node.values):
                config_reads.add("*")

            for child in reversed(list(ast.iter_child_nodes(node))):
                stack.append((child, child_depth, name, scope, in_copy_source or child is copy_source))


def extract_facts(text: str, path: str) -> SourceFacts:
    tokens = path_tokens(text)
    config_reads = {Path(token).name.removesuffix(".conf") for token in tokens if token.endswith(".conf")}
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError as error:
        return SourceFacts((), tokens, frozenset(config_reads), InvalidPython(error.msg))
    walk = _SourceFactWalk(tree, config_reads)
    imports: list[tuple[int, str, tuple[str, ...]]] = []
    conf_modules: set[str] = set()
    conf_functions: set[str] = set()
    # Group the imports by depth to preserve breadth-first order.
    for node in chain.from_iterable(walk.imports_by_depth[depth] for depth in sorted(walk.imports_by_depth)):
        if isinstance(node, ast.Import):
            imports.extend((0, alias.name, ()) for alias in node.names)
            conf_modules.update(alias.asname or alias.name for alias in node.names
                                if alias.name in {"conf", "scripts.lib.conf"})
        else:
            imports.append((node.level, node.module or "", tuple(alias.name for alias in node.names)))
            if not node.level and node.module == "scripts.lib":
                conf_modules.update(alias.asname or alias.name for alias in node.names if alias.name == "conf")
            elif not node.level and node.module == "scripts.lib.conf":
                conf_functions.update(alias.asname or alias.name for alias in node.names)
    for function, first in walk.config_calls:
        reader = isinstance(function, ast.Name) and function.id in conf_functions
        if isinstance(function, ast.Attribute):
            parts: list[str] = []
            module = function.value
            while isinstance(module, ast.Attribute):
                parts.append(module.attr)
                module = module.value
            if isinstance(module, ast.Name):
                reader = ".".join((module.id, *reversed(parts))) in conf_modules
        if reader:
            config_reads.add(Path(first).name.removesuffix(".conf"))
    return SourceFacts(tuple(imports), tokens, frozenset(config_reads),
                       scan_scopes=source_scan_scopes(walk.definitions, walk.ls_files_calls))


def indexed_files(repo: Path) -> tuple[set[str], dict[str, str]]:
    tracked: set[str] = set()
    blobs: dict[str, str] = {}
    for entry in git_output(repo, "ls-files", "--stage", "-z").split("\0"):
        if not entry:
            continue
        metadata, path = entry.split("\t", 1)
        mode, blob, stage = metadata.split()
        if stage != "0":
            raise GraphError(f"unmerged index entry: {path}")
        tracked.add(path)
        # A symlink's blob hashes its target name, not the text we read through it.
        if mode in {"100644", "100755"}:
            blobs[path] = blob
    return tracked, blobs


def encoded_scan_path(value: ScanPath | UnrootedPath) -> object:
    if isinstance(value, UnrootedPath):
        return None
    match value.root:
        case SourceFileRoot():
            root = ["file"]
        case AbsolutePathRoot(path):
            root = ["absolute", path]
        case ParameterRoot(name):
            root = ["parameter", name]
        case ImportedRoot(target):
            root = ["import", target]
    return [root, value.parts]


def scan_strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(part, str) for part in cast(list[object], value)):
        raise ValueError("invalid scan strings")
    return tuple(cast(list[str], value))


def scan_rows(value: object, length: int) -> Iterator[list[object]]:
    if not isinstance(value, list):
        raise ValueError("invalid scan rows")
    for row in cast(list[object], value):
        if not isinstance(row, list) or len(cast(list[object], row)) != length:
            raise ValueError("invalid scan row")
        yield cast(list[object], row)


def decoded_scan_path(value: object) -> ScanPath | UnrootedPath:
    if value is None:
        return UnrootedPath()
    if not isinstance(value, list) or len(cast(list[object], value)) != 2:
        raise ValueError("invalid scan path")
    root, parts = cast(list[object], value)
    names = scan_strings(root)
    origin: SourceFileRoot | AbsolutePathRoot | ParameterRoot | ImportedRoot
    if names == ("file",):
        origin = SourceFileRoot()
    elif len(names) == 2 and names[0] == "absolute":
        origin = AbsolutePathRoot(names[1])
    elif len(names) == 2 and names[0] == "parameter":
        origin = ParameterRoot(names[1])
    elif len(names) == 2 and names[0] == "import":
        origin = ImportedRoot(names[1])
    else:
        raise ValueError("invalid scan root")
    return ScanPath(origin, scan_strings(parts))


def decoded_scan_scopes(value: object, encoded_paths: object, encoded_bindings: object) -> tuple[ScanScope, ...]:
    if not isinstance(encoded_paths, list):
        raise ValueError("invalid scan paths")
    directories = tuple(decoded_scan_path(path) for path in cast(list[object], encoded_paths))

    def directory_at(index: object) -> ScanPath | UnrootedPath:
        if not isinstance(index, int) or not 0 <= index < len(directories):
            raise ValueError("invalid scan path index")
        return directories[index]

    if not isinstance(encoded_bindings, list):
        raise ValueError("invalid scan bindings")
    bindings: list[SourceBindings] = []
    for encoded in cast(list[object], encoded_bindings):
        paths: list[tuple[str, ScanPath | UnrootedPath]] = []
        for key, path in scan_rows(encoded, 2):
            if not isinstance(key, str):
                raise ValueError("invalid scan binding")
            paths.append((key, directory_at(path)))
        bindings.append(tuple(paths))

    result: list[ScanScope] = []
    for name, parameters, binding, scans, calls in scan_rows(value, 5):
        if not isinstance(name, str):
            raise ValueError("invalid scan scope name")
        if type(binding) is not int or not 0 <= binding < len(bindings):
            raise ValueError("invalid scan binding index")
        patterns: list[ScanPattern] = []
        for path, pattern, recursive in scan_rows(scans, 3):
            directory = directory_at(path)
            if not isinstance(pattern, str) or not isinstance(recursive, bool):
                raise ValueError("invalid scan pattern")
            patterns.append(ScanPattern(directory, pattern, recursive))
        invocations: list[ScanCall] = []
        for target, arguments, keywords in scan_rows(calls, 3):
            if not isinstance(target, str) or not isinstance(arguments, list):
                raise ValueError("invalid scan call")
            named: list[tuple[str, ScanPath | UnrootedPath]] = []
            for key, path in scan_rows(keywords, 2):
                if not isinstance(key, str):
                    raise ValueError("invalid scan keyword")
                named.append((key, directory_at(path)))
            invocations.append(ScanCall(target, tuple(directory_at(path) for path in cast(list[object], arguments)), tuple(named)))
        result.append(ScanScope(name, scan_strings(parameters), bindings[binding], tuple(patterns), tuple(invocations)))
    return tuple(result)


def encoded_scans(scopes: tuple[ScanScope, ...]) -> tuple[list[object], list[object], list[object]]:
    # Store each scan path once per blob, including repeated call arguments.
    paths: dict[ScanPath | UnrootedPath, int] = {}
    bindings: dict[SourceBindings, int] = {}

    def path_index(path: ScanPath | UnrootedPath) -> int:
        return paths.setdefault(path, len(paths))

    rows: list[object] = [
        [scope.name, scope.parameters,
         bindings.setdefault(scope.bindings, len(bindings)),
         [[path_index(scan.directory), scan.pattern, scan.recursive] for scan in scope.scans],
         [[call.target, [path_index(path) for path in call.arguments],
           [[name, path_index(path)] for name, path in call.keywords]] for call in scope.calls]]
        for scope in scopes
    ]
    binding_rows: list[object] = []
    for binding in bindings:
        binding_rows.append([[name, path_index(path)] for name, path in binding])
    return rows, [encoded_scan_path(path) for path in paths], binding_rows


def decoded_facts(value: object) -> SourceFacts:
    if not isinstance(value, dict):
        raise ValueError("invalid source facts")
    row = cast(dict[str, object], value)
    imports, tokens, config_reads = row.get("imports"), row.get("tokens"), row.get("config_reads")
    syntax_error = row.get("syntax_error")
    if (not isinstance(imports, list) or not isinstance(tokens, list)
            or not all(isinstance(token, str) for token in cast(list[object], tokens))
            or not isinstance(config_reads, list)
            or not all(isinstance(name, str) for name in cast(list[object], config_reads))
            or syntax_error is not None and not isinstance(syntax_error, str)):
        raise ValueError("invalid source facts fields")
    decoded: list[tuple[int, str, tuple[str, ...]]] = []
    for item in cast(list[object], imports):
        if not isinstance(item, list) or len(cast(list[object], item)) != 3:
            raise ValueError("invalid source import")
        level, module, names = cast(list[object], item)
        if (not isinstance(level, int) or level < 0 or not isinstance(module, str)
                or not isinstance(names, list)
                or not all(isinstance(name, str) for name in cast(list[object], names))):
            raise ValueError("invalid source import fields")
        decoded.append((level, module, tuple(cast(list[str], names))))
    syntax = InvalidPython(syntax_error) if isinstance(syntax_error, str) else ValidPython()
    return SourceFacts(tuple(decoded), frozenset(cast(list[str], tokens)), frozenset(cast(list[str], config_reads)),
                       syntax, decoded_scan_scopes(row.get("scan_scopes"), row.get("scan_paths"), row.get("scan_bindings")))


def read_fact_cache(path: Path) -> dict[str, SourceFacts] | None:
    try:
        uri = path.absolute().as_uri() + "?mode=ro&immutable=1"
        with closing(sqlite3.connect(uri, uri=True, timeout=0)) as database:
            versions = cast(list[tuple[int, int]], database.execute("SELECT format, extractor FROM cache_version").fetchall())
            if versions != [(CACHE_FORMAT_VERSION, EXTRACTOR_VERSION)]:
                return None
            status = cast(tuple[str] | None, database.execute("PRAGMA quick_check(1)").fetchone())
            if status != ("ok",):
                return None
            rows = cast(Iterator[tuple[object, object, object]],
                        database.execute("SELECT blob, payload, digest FROM facts"))
            result: dict[str, SourceFacts] = {}
            for blob, payload, digest in rows:
                if not isinstance(blob, str) or not isinstance(payload, bytes) or not isinstance(digest, bytes):
                    return None
                if fact_digest(blob, payload) != digest:
                    return None
                result[blob] = decoded_facts(cast(object, json.loads(payload)))
            return result
    except (OSError, ValueError, RecursionError, sqlite3.Error):
        return None


def fact_digest(blob: str, payload: bytes) -> bytes:
    digest = sha256(blob.encode("utf-8") + b"\0")
    digest.update(payload)
    return digest.digest()


def fact_row(blob: str, facts: SourceFacts) -> tuple[str, bytes, bytes]:
    scopes, paths, bindings = encoded_scans(facts.scan_scopes)
    payload = json.dumps({"imports": facts.imports, "tokens": sorted(facts.tokens),
                          "config_reads": sorted(facts.config_reads),
                          "scan_scopes": scopes, "scan_paths": paths, "scan_bindings": bindings,
                          "syntax_error": facts.syntax.message if isinstance(facts.syntax, InvalidPython) else None},
                         separators=(",", ":")).encode("utf-8")
    return blob, payload, fact_digest(blob, payload)


def write_fact_cache(path: Path, facts: dict[str, SourceFacts], published: set[str] | None,
                     retained: set[str]) -> bool:
    original = published if published is not None else set[str]()
    current = facts.keys() & retained
    added = current - original
    removed = original - current
    saved = False
    try:
        with tempfile.TemporaryDirectory(prefix=".affected-facts-", dir=path.parent) as directory:
            temporary = Path(directory) / path.name
            if published is not None:
                _ = shutil.copyfile(path, temporary)
            with closing(sqlite3.connect(temporary, timeout=0)) as database, database:
                if published is None:
                    _ = database.execute("CREATE TABLE cache_version (format INTEGER NOT NULL, extractor INTEGER NOT NULL)")
                    _ = database.execute("INSERT INTO cache_version VALUES (?, ?)", (CACHE_FORMAT_VERSION, EXTRACTOR_VERSION))
                    _ = database.execute("CREATE TABLE facts (blob TEXT PRIMARY KEY, payload BLOB NOT NULL, "
                                         + "digest BLOB NOT NULL) WITHOUT ROWID")
                _ = database.executemany("DELETE FROM facts WHERE blob = ?", ((blob,) for blob in removed))
                _ = database.executemany("INSERT INTO facts VALUES (?, ?, ?)", (fact_row(blob, facts[blob]) for blob in added))
            os.replace(temporary, path)
            saved = True
    except (OSError, sqlite3.Error):
        # A cleanup error after replacement leaves the new snapshot published.
        pass
    return saved


@dataclass
class FactCache:
    facts: dict[str, SourceFacts]
    writable_path: Path | None = None
    published: set[str] | None = None
    run: SelectionRun | None = None

    def retain(self, blobs: set[str]) -> None:
        if self.writable_path is not None and (self.facts.keys() & blobs) != self.published:
            saved = write_fact_cache(self.writable_path, self.facts, self.published, blobs)
            if self.run is not None:
                self.run.lock = FactCacheLock.PUBLISHED if saved else FactCacheLock.PUBLISH_FAILED


@contextmanager
def fact_cache(state_dir: Path | None, run: SelectionRun | None = None) -> Generator[FactCache]:
    if state_dir is None:
        yield FactCache({})
        return

    path = state_dir / FACT_CACHE_NAME
    with ExitStack() as stack:
        writable_path: Path | None = None
        lock_result = FactCacheLock.UNAVAILABLE
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            lock = stack.enter_context(path.with_suffix(".lock").open("ab"))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            writable_path = path
            lock_result = FactCacheLock.TAKEN
        except BlockingIOError:
            lock_result = FactCacheLock.BUSY
        except OSError:
            # Readers can use either published snapshot during an atomic replace.
            pass
        started = monotonic()
        snapshot = read_fact_cache(path)
        if run is not None:
            run.cache_read_s = monotonic() - started
            if lock_result is FactCacheLock.BUSY and snapshot is not None:
                lock_result = FactCacheLock.BUSY_SNAPSHOT
            run.lock = lock_result
        published = set(snapshot) if writable_path is not None and snapshot is not None else None
        yield FactCache(snapshot if snapshot is not None else {}, writable_path, published, run)


def module_dependencies(module: str, tracked: set[str]) -> set[str]:
    parts = module.split(".")
    prefix = "/".join(parts)
    candidates = {f"{prefix}.py", f"{prefix}/__init__.py"}
    candidates.update("/".join(parts[:index]) + "/__init__.py" for index in range(1, len(parts)))
    return candidates & tracked


def revision_files(repo: Path, revision: str) -> dict[str, str]:
    return {path: entry.object for path, entry in parse_tree_listing(git_output(repo, "ls-tree", "-rz", revision)).items()
            if entry.kind == "blob"}


def blob_texts(repo: Path, blobs: set[str]) -> dict[str, str]:
    try:
        result = subprocess.run(["git", "-C", str(repo), "cat-file", "--batch"],
                                input=("\n".join(sorted(blobs)) + "\n").encode(), capture_output=True, check=False)
    except OSError as error:
        raise GraphError(str(error)) from error
    if result.returncode:
        raise GraphError(result.stderr.decode("utf-8", errors="replace").strip() or "git cat-file failed")
    replies = BytesIO(result.stdout)
    texts: dict[str, str] = {}
    for blob in sorted(blobs):
        reply = read_batch_blob(replies)
        if not isinstance(reply, GitBlob):
            raise GraphError(f"{blob}: invalid Git blob response")
        texts[blob] = reply.content.decode("utf-8")
    return texts


def dirty_paths(repo: Path) -> set[str]:
    return parse_status_z(git_output(repo, "--no-optional-locks", "status", "--porcelain", "-z", "--untracked-files=all"))


def worktree_files(repo: Path, dirty: set[str]) -> tuple[set[str], dict[str, str]]:
    indexed, blobs = indexed_files(repo)
    paths = {path for path in indexed | dirty if path.endswith(".py")}
    return {path for path in paths if (repo / path).is_file()}, blobs


@dataclass(frozen=True)
class RepositoryDirectory:
    path: str


type ScanScopeKey = tuple[str, str]
type ResolvedDirectory = RepositoryDirectory | UnrootedPath | OutsideRepository
type ImportedBinding = tuple[ScanScopeKey, ScanPath | UnrootedPath]
type ScanBindings = frozenset[tuple[str, ResolvedDirectory]]
type ScanRequest = tuple[ScanScopeKey, ScanBindings, bool, frozenset[ScanScopeKey]]
type ScanTarget = tuple[str, ScanScope, dict[str, ResolvedDirectory], bool]


def unresolved_scan_paths(facts: dict[str, SourceFacts]) -> bool:
    """Check whether pruning could suppress an error from path resolution."""
    checked: set[ScanPath] = set()
    for source in facts.values():
        for scope in source.scan_scopes:
            paths = chain((path for _, path in scope.bindings), (scan.directory for scan in scope.scans),
                          (path for call in scope.calls for path in call.arguments),
                          (path for call in scope.calls for _, path in call.keywords))
            for value in paths:
                if isinstance(value, ScanPath) and isinstance(value.root, AbsolutePathRoot) and value not in checked:
                    try:
                        _ = Path(value.root.path).joinpath(*value.parts).resolve()
                    except (OSError, ValueError):
                        return True
                    checked.add(value)
    return False


def scan_components(predecessors: dict[ScanScopeKey, set[ScanScopeKey]],
                    successors: dict[ScanScopeKey, set[ScanScopeKey]],
                    roots: set[ScanScopeKey]) -> dict[ScanScopeKey, frozenset[ScanScopeKey]]:
    """Find components of scopes that can reach a scan."""
    finished: list[ScanScopeKey] = []
    visited: set[ScanScopeKey] = set()
    for root in roots:
        if root in visited:
            continue
        visited.add(root)
        stack = [(root, iter(predecessors[root]))]
        while stack:
            key, edges = stack[-1]
            parent = next(edges, None)
            if parent is None:
                finished.append(key)
                _ = stack.pop()
            elif parent not in visited:
                visited.add(parent)
                stack.append((parent, iter(predecessors[parent])))

    components: dict[ScanScopeKey, frozenset[ScanScopeKey]] = {}
    for root in reversed(finished):
        if root not in visited:
            continue
        pending = [root]
        members = {root}
        visited.remove(root)
        while pending:
            key = pending.pop()
            new = successors[key] & visited
            visited.difference_update(new)
            members.update(new)
            pending.extend(new)
        component = frozenset(members)
        components.update((key, component) for key in component)
    return components


def repository_scan_graph(repo: Path, facts: dict[str, SourceFacts], bare_modules: dict[str, set[str]]
                          ) -> dict[str, frozenset[RepositoryScan]]:
    """Resolve scoped scans without following imports beyond a called helper."""
    repository_root = repo.resolve()
    scopes = {path: {scope.name: scope for scope in source.scan_scopes} for path, source in facts.items()}

    def find_symbols(source: str, target: str) -> Iterator[ScanScopeKey]:
        level = len(target) - len(target.lstrip("."))
        parts = target.lstrip(".").split(".")
        if level:
            parents = Path(source).parent.parts
            parts = [*parents[:len(parents) - level + 1], *parts]
        if parts[0] != "scripts":
            for path in sorted(bare_modules.get(parts[0], set())):
                yield path, ".".join(parts[1:])
        for size in range(len(parts), 0, -1):
            prefix = "/".join(parts[:size])
            found = [path for path in (prefix + ".py", prefix + "/__init__.py") if path in scopes]
            if found:
                for path in found:
                    yield path, ".".join(parts[size:])
                return

    @cache
    def symbols(source: str, target: str) -> tuple[ScanScopeKey, ...]:
        return tuple(find_symbols(source, target))

    def imported_bindings(source: str, target: str, seen: set[ScanScopeKey]) -> Iterator[ImportedBinding]:
        for key in symbols(source, target):
            if key in seen:
                continue
            path, name = key
            parent, _, member = name.rpartition(".")
            scope = scopes[path].get(parent)
            if scope is not None and (binding := dict(scope.bindings).get(member)) is not None:
                yield key, binding

    def directory(source: str, value: ScanPath | UnrootedPath,
                  parameters: dict[str, ResolvedDirectory]) -> ResolvedDirectory:
        if isinstance(value, UnrootedPath):
            return value
        seen: set[ScanScopeKey] = set()
        stack: list[tuple[ScanScopeKey, tuple[str, ...], Iterator[ImportedBinding]]] = []
        while True:
            parts: tuple[str, ...] | None = None
            candidates: Iterator[ImportedBinding] | None = None
            origin: ResolvedDirectory = UnrootedPath()
            if isinstance(value, ScanPath):
                parts = value.parts
                match value.root:
                    case SourceFileRoot():
                        origin = RepositoryDirectory(source)
                    case AbsolutePathRoot(path):
                        absolute = Path(path).joinpath(*parts).resolve()
                        try:
                            origin = RepositoryDirectory(absolute.relative_to(repository_root).as_posix())
                        except ValueError:
                            origin = OutsideRepository()
                        parts = None
                    case ParameterRoot(name):
                        origin = parameters.get(name, OutsideRepository())
                    case ImportedRoot(target):
                        candidates = imported_bindings(source, target, seen)

            while True:
                if candidates is not None and not isinstance(origin, RepositoryDirectory):
                    candidate = next(candidates, None)
                    if candidate is not None:
                        key, value = candidate
                        assert parts is not None
                        stack.append((key, parts, candidates))
                        seen.add(key)
                        source = key[0]
                        parameters = {}
                        break
                if isinstance(origin, RepositoryDirectory) and parts is not None:
                    # Check each suffix before the parent uses this directory.
                    path = posixpath.normpath(posixpath.join(origin.path, *parts))
                    if path == ".." or path.startswith(("../", "/")):
                        origin = OutsideRepository()
                    else:
                        origin = RepositoryDirectory(path)
                if not stack:
                    return origin
                key, parts, candidates = stack.pop()
                seen.remove(key)

    def direct_scans(source: str, scope: ScanScope, parameters: dict[str, ResolvedDirectory]) -> Iterator[RepositoryScan]:
        for scan in scope.scans:
            root = directory(source, scan.directory, parameters)
            if isinstance(root, UnrootedPath):
                yield RepositoryScan(".", scan.pattern, True)
            elif isinstance(root, RepositoryDirectory):
                yield RepositoryScan(root.path, scan.pattern, scan.recursive)

    def scan_arguments(source: str, call: ScanCall, parameters: dict[str, ResolvedDirectory]
                       ) -> tuple[list[ResolvedDirectory], dict[str, ResolvedDirectory]]:
        arguments = [directory(source, value, parameters) for value in call.arguments]
        keywords = {name: directory(source, value, parameters) for name, value in call.keywords}
        return arguments, keywords

    if (unresolved_scan_paths(facts)
            or any(len(source.scan_scopes) != len(scopes[path]) for path, source in facts.items())):
        def all_scan_targets(source: str, scope: ScanScope, parameters: dict[str, ResolvedDirectory],
                             follow_imports: bool) -> Iterator[ScanTarget]:
            for call in scope.calls:
                arguments, keywords = scan_arguments(source, call, parameters)
                local = call.target in scopes[source]
                targets: tuple[ScanScopeKey, ...] = ((source, call.target),) if local else ()
                if not local and follow_imports and any(isinstance(value, RepositoryDirectory)
                                                        for value in (*arguments, *keywords.values())):
                    targets = symbols(source, call.target)
                for path, name in targets:
                    callee = scopes[path].get(name)
                    if callee is not None:
                        bound = {**dict(zip(callee.parameters, arguments)), **keywords}
                        yield path, callee, bound, follow_imports and local

        def all_called_scans(source: str, scope: ScanScope) -> set[RepositoryScan]:
            result: set[RepositoryScan] = set()
            visited: set[ScanScopeKey] = set()
            stack: list[tuple[ScanScopeKey, Iterator[ScanTarget]]] = []

            def visit(path: str, scope: ScanScope, parameters: dict[str, ResolvedDirectory], follow_imports: bool) -> None:
                key = path, scope.name
                if key not in visited:
                    visited.add(key)
                    result.update(direct_scans(path, scope, parameters))
                    stack.append((key, all_scan_targets(path, scope, parameters, follow_imports)))

            visit(source, scope, {}, True)
            while stack:
                key, targets = stack[-1]
                target = next(targets, None)
                if target is None:
                    _ = stack.pop()
                    visited.remove(key)
                else:
                    visit(*target)
            return result

        return {path: frozenset(scan for scope in source.scan_scopes for scan in all_called_scans(path, scope))
                for path, source in facts.items()}

    all_scopes = {(path, scope.name): scope for path, names in scopes.items() for scope in names.values()}
    predecessors: dict[ScanScopeKey, set[ScanScopeKey]] = {key: set() for key in all_scopes}
    successors: dict[ScanScopeKey, set[ScanScopeKey]] = {key: set() for key in all_scopes}
    calls: dict[ScanScopeKey, list[tuple[ScanCall, bool, tuple[ScanScopeKey, ...]]]] = {}
    for key, scope in all_scopes.items():
        source = key[0]
        resolved: list[tuple[ScanCall, bool, tuple[ScanScopeKey, ...]]] = []
        for call in scope.calls:
            local = call.target in scopes[source]
            targets = (((source, call.target),) if local else
                       tuple(target for target in symbols(source, call.target) if target in all_scopes))
            if targets:
                resolved.append((call, local, targets))
                successors[key].update(targets)
                for target in targets:
                    predecessors[target].add(key)
        if resolved:
            calls[key] = resolved
    components = scan_components(predecessors, successors, {key for key, scope in all_scopes.items() if scope.scans})
    calls = {key: [(call, local, reachable) for call, local, targets in invocations
                   if (reachable := tuple(target for target in targets if target in components))]
             for key, invocations in calls.items() if key in components}

    def scan_requests(key: ScanScopeKey, parameters: dict[str, ResolvedDirectory],
                      follow_imports: bool, seen: frozenset[ScanScopeKey]) -> Iterator[ScanRequest]:
        source = key[0]
        seen = seen | {key}
        for call, local, targets in calls.get(key, ()):
            if not local and not follow_imports:
                continue
            arguments, keywords = scan_arguments(source, call, parameters)
            if not local and not any(isinstance(value, RepositoryDirectory) for value in (*arguments, *keywords.values())):
                continue
            for target in targets:
                if target not in seen:
                    bound = {**dict(zip(all_scopes[target].parameters, arguments)), **keywords}
                    # A path can return to a visited scope only within the same component.
                    yield target, frozenset(bound.items()), follow_imports and local, seen & components[target]

    results: dict[ScanRequest, frozenset[RepositoryScan]] = {}

    def scan_frame(request: ScanRequest) -> tuple[ScanRequest, set[RepositoryScan], Iterator[ScanRequest]]:
        key, bindings, follow_imports, seen = request
        parameters = dict(bindings)
        return (request, set(direct_scans(key[0], all_scopes[key], parameters)),
                scan_requests(key, parameters, follow_imports, seen))

    def called_scans(request: ScanRequest) -> frozenset[RepositoryScan]:
        if request in results:
            return results[request]
        stack = [scan_frame(request)]
        while stack:
            current, scans, children = stack[-1]
            child = next(children, None)
            if child is None:
                results[current] = frozenset(scans)
                _ = stack.pop()
                if stack:
                    stack[-1][1].update(scans)
            elif child in results:
                scans.update(results[child])
            else:
                stack.append(scan_frame(child))
        return results[request]

    no_bindings: ScanBindings = frozenset()
    no_visited: frozenset[ScanScopeKey] = frozenset()
    graph = {path: frozenset(scan for scope in source.scan_scopes
                             if (key := (path, scope.name)) in components
                             for scan in called_scans((key, no_bindings, True, no_visited)))
             for path, source in facts.items()}
    return graph


def graph_for_files(repo: Path, paths: set[str], blobs: dict[str, str], dirty: set[str],
                    cached: dict[str, SourceFacts], *, worktree: bool, run: SelectionRun | None = None
                    ) -> SourceGraph:
    bare_modules: dict[str, set[str]] = defaultdict(set)
    for path in paths:
        name = Path(path).parent.name if Path(path).name == "__init__.py" else Path(path).stem
        bare_modules[name].add(path)
    sources = sorted(paths)
    texts: dict[str, str] = {}
    if not worktree:
        missing = {blobs[path] for path in sources if path in blobs and blobs[path] not in cached}
        if missing:
            if run is not None:
                run.cache_misses += len(missing)
            started = monotonic()
            try:
                texts = blob_texts(repo, missing)
            finally:
                if run is not None:
                    run.extraction_s += monotonic() - started
    external = set(sys.stdlib_module_names) | set(packages_distributions())
    graph: dict[str, set[str]] = {}
    tokens: dict[str, frozenset[str]] = {}
    config_reads: dict[str, frozenset[str]] = {}
    scan_facts: dict[str, SourceFacts] = {}
    for path in sources:
        blob = blobs.get(path) if path not in dirty else None
        facts = cached.get(blob) if blob is not None else None
        if facts is None:
            if worktree and run is not None:
                run.cache_misses += 1
            started = monotonic()
            try:
                text = (repo / path).read_text(encoding="utf-8") if worktree else texts[blobs[path]]
                if run is not None:
                    run.extracted_files += 1
                facts = extract_facts(text, path)
            finally:
                if run is not None:
                    run.extraction_s += monotonic() - started
            if blob is not None:
                cached[blob] = facts
        elif run is not None:
            run.cache_hits += 1
        if isinstance(facts.syntax, InvalidPython) and is_test_module(path):
            raise SyntaxError(f"{facts.syntax.message} ({path})")
        if worktree:
            tokens[path] = facts.tokens
            config_reads[path] = facts.config_reads
        scan_facts[path] = facts
        dependencies: set[str] = set()
        for level, module, names in facts.imports:
            if level:
                parents = Path(path).parent.parts
                module = ".".join((*parents[:len(parents) - level + 1], *module.split("."))).rstrip(".")
            for imported in (module, *(f"{module}.{name}" for name in names)):
                root = imported.split(".")[0]
                if root == "scripts" or root and root not in external:
                    imported_paths = module_dependencies(imported, paths)
                    if root != "scripts":
                        imported_paths.update(bare_modules.get(root, set()))
                    dependencies.update(imported_paths)

        dependencies.discard(path)
        graph[path] = dependencies
    scans = repository_scan_graph(repo, scan_facts, bare_modules) if worktree or unresolved_scan_paths(scan_facts) else {}
    return SourceGraph(graph, tokens, config_reads, scans)


MODULE_HISTORY_SQL = (
    "WITH eligible AS NOT MATERIALIZED (SELECT id, started_at FROM gate_runs "
    + "WHERE selection = 'full' AND aborted = 0 AND host IS NOT ?), latest AS (SELECT "
    + "(SELECT id FROM eligible WHERE "
    + "EXISTS (SELECT 1 FROM gate_modules AS recorded WHERE recorded.run_id = eligible.id "
    + "AND recorded.config_reads IS NOT NULL) ORDER BY started_at DESC, id DESC LIMIT 1) AS config_gate, "
    + "(SELECT id FROM eligible WHERE "
    + "EXISTS (SELECT 1 FROM gate_modules AS recorded WHERE recorded.run_id = eligible.id "
    + "AND recorded.folder_scans IS NOT NULL) ORDER BY started_at DESC, id DESC LIMIT 1) AS folder_gate, "
    + "(SELECT id FROM eligible WHERE "
    + "EXISTS (SELECT 1 FROM gate_modules AS recorded WHERE recorded.run_id = eligible.id "
    + "AND recorded.content_reads IS NOT NULL) ORDER BY started_at DESC, id DESC LIMIT 1) AS content_gate) "
    + "SELECT suite, module, CASE WHEN run_id = config_gate THEN config_reads END, "
    + "CASE WHEN run_id = folder_gate THEN folder_scans END, "
    + "CASE WHEN run_id = content_gate THEN content_reads END FROM latest CROSS JOIN gate_modules "
    + "WHERE run_id IN (config_gate, folder_gate, content_gate) ORDER BY suite, module, attempt"
)


def latest_module_reads() -> dict[str, ModuleReadRecord]:
    """Read each field from its newest eligible full gate."""
    from scripts.buildlog import index

    _ = index.update()
    with closing(index.read_only()) as connection:
        rows = cast(list[tuple[str, str, str | None, str | None, str | None]],
                    connection.execute(MODULE_HISTORY_SQL, (MAC_LINUX_TARGET,)).fetchall())
    recorded: dict[str, ModuleReadRecord] = {}
    for suite, module, config_json, folder_json, content_json in rows:
        path = f"scripts/{suite}/{module}"
        reads = module_read_record(json.loads(config_json) if config_json is not None else None,
                                   json.loads(folder_json) if folder_json is not None else None,
                                   json.loads(content_json) if content_json is not None else None)
        previous = recorded.get(path, ModuleReadRecord(NoConfigReadRecord(), NoFolderScanRecord(), NoContentReadRecord()))
        configs = previous.config_reads
        if isinstance(reads.config_reads, RecordedConfigReads):
            stems = reads.config_reads.stems
            if isinstance(configs, RecordedConfigReads):
                stems |= configs.stems
            configs = RecordedConfigReads(stems)
        folders = previous.folder_scans
        if isinstance(reads.folder_scans, RecordedFolderScans):
            scans = reads.folder_scans.folders
            if isinstance(folders, RecordedFolderScans):
                scans |= folders.folders
            folders = RecordedFolderScans(scans)
        contents = previous.content_reads
        if isinstance(reads.content_reads, RecordedContentReads):
            paths = reads.content_reads.paths
            if isinstance(contents, RecordedContentReads):
                paths |= contents.paths
            contents = RecordedContentReads(paths)
        recorded[path] = ModuleReadRecord(configs, folders, contents)
    return recorded


def changed_path_parents(repo: Path, changed: set[str], base_paths: set[str]) -> set[str]:
    """Include parents only for added and removed paths."""
    directories = {"."}
    for path in base_paths:
        directories.update(parent.as_posix() for parent in Path(path).parents)
    parents: set[str] = set()
    for path in changed:
        if path in base_paths and os.path.lexists(repo / path):
            continue
        parent = Path(path).parent
        while parent.as_posix() not in directories:
            parent = parent.parent
        parents.add(parent.as_posix())
        parent = Path(path).parent
        while parent != Path(".") and not (repo / parent).is_dir():
            parent = parent.parent
        parents.add(parent.as_posix())
    return parents


def scans_changed_paths(scans: RecordedFolderScans, changed: set[str], parents: set[str]) -> bool:
    for folder in scans.folders:
        match folder:
            case WholeRepository():
                return True
            case ListedDirectory(path):
                if path in parents:
                    return True
            case RecursiveDirectory(prefix):
                if any(path == prefix or path.startswith(prefix + "/") for path in changed):
                    return True
    return False


def source_blobs(paths: set[str], blobs: dict[str, str]) -> set[str]:
    return {blob for path, blob in blobs.items() if path in paths}


def dependency_graph(repo: Path, *, state_dir: Path | None = None
                     ) -> tuple[dict[str, set[str]], dict[str, frozenset[str]]]:
    dirty = dirty_paths(repo)
    paths, blobs = worktree_files(repo, dirty)
    with fact_cache(state_dir) as cached:
        result = graph_for_files(repo, paths, blobs, dirty, cached.facts, worktree=True)
        cached.retain(source_blobs(paths | dirty, blobs))
    return result.imports, result.tokens


def folder_names_of(paths: set[str]) -> dict[str, set[str]]:
    folder_names: dict[str, set[str]] = defaultdict(set)
    for path in paths:
        parent = str(Path(path).parent)
        if parent != ".":
            folder_names[parent].add(Path(path).name)
    return folder_names


def names_path(tokens: frozenset[str], source: str, paths: set[str],
               folder_names: dict[str, set[str]]) -> bool:
    """Match a complete path, or a file name with its folder."""
    if tokens & paths:
        return True
    parent = str(Path(source).parent)
    return any(tokens & names and (parent == folder or Path(folder).name in tokens)
               for folder, names in folder_names.items())


def fixture_sources(imports: dict[str, set[str]], path: str) -> set[str]:
    """Include imported test fixtures and their imported test fixtures."""
    pending = [path]
    seen: set[str] = set()
    while pending:
        source = pending.pop()
        if source in seen:
            continue
        seen.add(source)
        pending.extend(dependency for dependency in imports.get(source, set())
                       if dependency.endswith("_test_support.py") or is_test_module(dependency))
    return seen


def names_changed_file(graph: SourceGraph, sources: set[str], changed: set[str],
                       folder_names: dict[str, set[str]]) -> bool:
    """Match a changed fixture, its imports, or its named paths."""
    return any(source in changed or graph.imports.get(source, set()) & changed
               or names_path(graph.tokens.get(source, frozenset()), source, changed, folder_names)
               for source in sources)


def declared_scan_roots(repo: Path, modules: Sequence[Path], words: Sequence[str]) -> dict[str, frozenset[str]]:
    """Validate declared repository paths for live gate modules."""
    live_modules = {module.relative_to(repo).as_posix() for module in modules
                    if is_test_module(module.name) and module.is_file()}
    roots: dict[str, set[str]] = defaultdict(set)
    for word in words:
        if word.count(":") != 1:
            raise ValueError(f"invalid scan_roots entry {word!r}: expected exactly one ':'")
        module, folder = word.split(":")
        module_path, folder_path = PurePosixPath(module), PurePosixPath(folder)
        if not module.endswith(".py") or module_path.is_absolute() or ".." in module_path.parts:
            raise ValueError(f"invalid scan_roots entry {word!r}: expected a relative test module path")
        module_name = f"scripts/{module_path.as_posix()}"
        if module_name not in live_modules:
            raise ValueError(f"invalid scan_roots entry {word!r}: test module is not a live gate module")
        if not folder or folder_path.is_absolute() or ".." in folder_path.parts:
            raise ValueError(f"invalid scan_roots entry {word!r}: expected a relative repository path")
        if not (repo / folder_path).exists():
            raise ValueError(f"invalid scan_roots entry {word!r}: repository path does not exist")
        roots[module_name].add(folder_path.as_posix())
    return {module: frozenset(folders) for module, folders in roots.items()}


def scans_repository_changes(graph: SourceGraph, sources: set[str], changed: set[str],
                             module: str, declarations: dict[str, frozenset[str]]) -> bool:
    """Use declared paths when present, or retain inferred scan evidence."""
    if module in declarations:
        return any(PurePosixPath(path).is_relative_to(folder)
                   for folder in declarations[module] for path in changed)
    for source in sources:
        for scan in graph.repository_scans.get(source, frozenset()):
            for path in changed:
                try:
                    relative = PurePosixPath(path).relative_to(scan.directory)
                except ValueError:
                    continue
                pattern = "**/" + scan.pattern if scan.recursive else scan.pattern
                if relative.full_match(pattern):
                    return True
    return False


def is_script(repo: Path, path: str) -> bool:
    """A non-Python file under scripts/ that ends in .sh or has an execute bit."""
    return (path.startswith("scripts/") and not path.endswith(".py")
            and (path.endswith(".sh") or os.access(repo / path, os.X_OK)))


def calls_script(tokens: frozenset[str], caller: str, targets: set[str],
                 folder_names: dict[str, set[str]]) -> bool:
    """Shell calls also prefix a full path or a sibling name with a variable."""
    if names_path(tokens, caller, targets, folder_names):
        return True
    siblings = folder_names.get(str(Path(caller).parent), set())
    names = {Path(target).name for target in targets}
    for token in tokens:
        _, slash, name = token.rpartition("/")
        if slash and name in names and (name in siblings
                                        or any(token.endswith(f"/{target}") for target in targets)):
            return True
    return False


def script_callers(repo: Path, candidates: set[str], changed: set[str]) -> set[str]:
    """Return the changed scripts and every script that calls one, directly or through others."""
    found = {path for path in changed if is_script(repo, path)}
    if not found:
        return found
    scripts = {path: path_tokens((repo / path).read_text(encoding="utf-8", errors="replace"))
               for path in candidates if is_script(repo, path) and (repo / path).is_file()}
    pending = set(found)
    while pending:
        folder_names = folder_names_of(pending)
        pending = {caller for caller, tokens in scripts.items()
                   if caller not in found and calls_script(tokens, caller, pending, folder_names)}
        found |= pending
    return found


def select_affected(repo: Path, base: CertifiedBase | NoCertifiedBase,
                    modules: Sequence[Path], dirty: set[str], *,
                    state_dir: Path | None = None) -> AffectedSelection | FullSuite:
    started = monotonic()
    run = SelectionRun()
    try:
        settings = conf.read("gate")
        declarations = declared_scan_roots(repo, modules, settings.values().get("scan_roots", "").split())
        candidates = {base.commit: base} if isinstance(base, CertifiedBase) else {}
        for branch in settings.words("landing_branches"):
            ancestor = subprocess.run(["git", "-C", str(repo), "merge-base", "HEAD",
                                       f"refs/remotes/origin/{branch}"],
                                      capture_output=True, text=True, check=False)
            if ancestor.returncode:
                continue
            commit = ancestor.stdout.strip()
            if commit not in candidates:
                at = git_output(repo, "show", "-s", "--format=%cI", commit).strip()
                candidates[commit] = CertifiedBase(commit, at)
        if not candidates:
            return FullSuite("no certified base", run)
        differences: list[tuple[CertifiedBase, set[str]]] = []
        for candidate in candidates.values():
            changes = git_output(repo, "diff", "--name-only", "-z", "--no-renames", candidate.commit, "HEAD")
            differences.append((candidate, set(changes.split("\0")) - {""}))
        base, committed_changes = min(differences, key=lambda item: len(item[1]))
        changed = committed_changes | dirty
        paths, blobs = worktree_files(repo, dirty)
        with fact_cache(state_dir, run) as cached:
            with run.graph_build():
                graph = graph_for_files(repo, paths, blobs, dirty, cached.facts, worktree=True, run=run)
            config_paths = {path for path in changed if path.startswith("config/") and path.endswith(".conf")}
            configs = {Path(path).name.removesuffix(".conf") for path in config_paths}
            recorded = latest_module_reads() if changed else {}
            has_folder_records = any(isinstance(reads.folder_scans, RecordedFolderScans) for reads in recorded.values())
            has_content_records = any(isinstance(reads.content_reads, RecordedContentReads) for reads in recorded.values())
            content_changes = {path for path in changed if not path.startswith("config/")}
            python = {path for path in changed if path.endswith(".py")}
            removed = {path for path in python if not (repo / path).is_file()}
            needs_base_blobs = has_folder_records or bool(removed) or cached.writable_path is not None
            base_blobs = revision_files(repo, base.commit) if needs_base_blobs else {}
            scanned_parents = changed_path_parents(repo, changed, set(base_blobs)) if has_folder_records else set[str]()
            base_paths = {path for path in base_blobs if path.endswith(".py")}
            old_graph: dict[str, set[str]] = {}
            if removed:
                with run.graph_build():
                    old_graph = graph_for_files(repo, base_paths, base_blobs, set(), cached.facts, worktree=False, run=run).imports
            cached.retain(source_blobs(paths | dirty, blobs) | source_blobs(base_paths, base_blobs))
        folder_stems: dict[str, set[str]] = defaultdict(set)
        for source in python:
            if not is_test_module(source):
                folder_stems[str(Path(source).parent)].add(Path(source).stem)
        named = changed | script_callers(repo, set(blobs) | dirty, changed)
        folder_names = folder_names_of(named)
        selected: list[Path] = []
        for module in modules:
            path = module.relative_to(repo).as_posix()
            reads = recorded.get(path, ModuleReadRecord(NoConfigReadRecord(), NoFolderScanRecord(), NoContentReadRecord()))
            sources = fixture_sources(graph.imports, path)
            static_configs = {name for source in sources for name in graph.config_reads.get(source, frozenset())}
            config_reads = reads.config_reads
            config_changed = bool(configs) and (isinstance(config_reads, NoConfigReadRecord)
                or bool(config_reads.stems & configs) or bool(static_configs & configs) or "*" in static_configs
                or isinstance(reads.content_reads, RecordedContentReads) and bool(reads.content_reads.paths & config_paths))
            folder_changed = path not in declarations and has_folder_records and (isinstance(reads.folder_scans, NoFolderScanRecord)
                or scans_changed_paths(reads.folder_scans, changed, scanned_parents))
            content_changed = bool(content_changes) and has_content_records and (isinstance(reads.content_reads, NoContentReadRecord)
                or bool(reads.content_reads.paths & content_changes))
            if (path in changed or graph.imports.get(path, set()) & python
                    or old_graph.get(path, set()) & removed or names_changed_file(graph, sources, named, folder_names)
                    or scans_repository_changes(graph, sources, changed, path, declarations)
                    or config_changed or folder_changed or content_changed):
                selected.append(module)
                continue
            stems = folder_stems.get(str(Path(path).parent), set())
            if stems:
                text = module.read_text(encoding="utf-8")
                if any(re.search(rf"\b{re.escape(stem)}\b", text) for stem in stems):
                    selected.append(module)
        return AffectedSelection(base, tuple(sorted(changed)), tuple(selected), run)
    except (GraphError, OSError, ValueError, SyntaxError, sqlite3.Error) as error:
        return FullSuite(f"graph error: {error}", run)
    finally:
        run.total_s = monotonic() - started
