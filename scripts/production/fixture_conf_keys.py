#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Find missing fixture keys and report reads of removed or absent config keys.

A fixture text without the key makes the code under test fall back to the committed
value with a warning, or fail where no committed value exists, as on a box without git.
Each literal fixture text of a config file must hold each plain key that the tip adds to
that file. A per-name key (`kind.mac`, `value.natedev`) is not required, because a
fixture names only its own targets and hosts.

The report lists reads of removed keys and fixed-name reads of keys absent from the tip config files.
The report counts sources it cannot scan. The advisory scan does not affect the exit code.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import ast
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from enum import Enum, auto
from importlib.util import resolve_name
from pathlib import Path, PurePosixPath
import re
import sys
from typing import final, override

from scripts.lib import conf
from scripts.production.affected_modules import GraphError, blob_texts, git_output, scope_nodes
from scripts.production.gate_modules import is_test_module
from scripts.style.git_process import GitReadFailed, MissingBlob, blob_contents, parse_tree_listing


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
class RemovedKeyRead:
    source: str
    line: int
    key: str
    conf_names: tuple[str, ...]


@dataclass(frozen=True)
class AbsentKeyRead:
    source: str
    line: int
    key: str


@dataclass
class AdvisoryReads:
    removed: list[RemovedKeyRead] = field(default_factory=list)
    absent: list[AbsentKeyRead] = field(default_factory=list)
    sources_not_scanned: int = 0


@dataclass(frozen=True)
class KeyReport:
    new_keys: dict[str, set[str]]
    missing: list[MissingKey]
    not_checked: list[FixtureWrite]
    removed_keys: dict[str, set[str]]
    advisory: AdvisoryReads


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


def own_nodes(scope: Scope | ast.Lambda) -> Iterator[ast.AST]:
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


_LOADERS = {"read", "read_or_repository"}
_ACCESSORS = {"effective_key", "number", "origin", "text", "value", "values", "whole", "words"}


class ReadName(Enum):
    CONFIG_MODULE = auto()
    SCRIPTS_PACKAGE_WITH_CONF = auto()
    CONFIG_LOADER = auto()
    CONFIG_READER = auto()
    CONFIG_READER_TYPE = auto()


type ReadBindings = dict[str, set[ReadName]]
type KeyOccurrence = tuple[str, int]
type SourcePosition = tuple[int, int]


@dataclass
class SourceKeyReads:
    mentions: set[KeyOccurrence] = field(default_factory=set)
    fixed: set[KeyOccurrence] = field(default_factory=set)


def imported_name(item: ast.alias) -> str:
    return item.asname or item.name.partition(".")[0]


def is_config_module(node: ast.expr, names: ReadBindings) -> bool:
    if isinstance(node, ast.Name):
        return ReadName.CONFIG_MODULE in names.get(node.id, ())
    return (isinstance(node, ast.Attribute) and node.attr == "conf"
            and isinstance(node.value, ast.Attribute) and node.value.attr == "lib"
            and isinstance(node.value.value, ast.Name)
            and ReadName.SCRIPTS_PACKAGE_WITH_CONF in names.get(node.value.value.id, ()))


def is_config_loader(node: ast.expr, names: ReadBindings) -> bool:
    if isinstance(node, ast.Name):
        return ReadName.CONFIG_LOADER in names.get(node.id, ())
    return (isinstance(node, ast.Attribute) and node.attr in _LOADERS
            and is_config_module(node.value, names))


def config_reader_parameter(annotation: ast.expr | None, names: ReadBindings) -> bool:
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            annotation = ast.parse(annotation.value, mode="eval").body
        except SyntaxError:
            return False
    if isinstance(annotation, ast.Name):
        return ReadName.CONFIG_READER_TYPE in names.get(annotation.id, ())
    return (isinstance(annotation, ast.Attribute) and annotation.attr == "ConfigRead"
            and is_config_module(annotation.value, names))


@dataclass(frozen=True)
class FixedReadCall:
    receiver: ast.expr
    keys: Sequence[ast.expr]


@dataclass(frozen=True)
class KeyAssignment:
    position: SourcePosition
    keys: tuple[str, ...]


@dataclass(frozen=True)
class LoopKeyAssignment(KeyAssignment):
    body_end: SourcePosition


@dataclass(frozen=True)
class LoopBody:
    end: SourcePosition
    known_empty: bool


def source_end(node: ast.expr | ast.stmt) -> SourcePosition:
    return node.end_lineno or node.lineno, node.end_col_offset or node.col_offset


def literal_loop_keys(iterable: ast.expr) -> tuple[str, ...]:
    if isinstance(iterable, ast.Constant) and isinstance(iterable.value, str):
        return tuple(iterable.value)
    if isinstance(iterable, ast.Dict):
        items = iterable.keys
    elif isinstance(iterable, ast.List | ast.Set | ast.Tuple):
        items = iterable.elts
    else:
        return ()
    return tuple(item.value for item in items if isinstance(item, ast.Constant) and isinstance(item.value, str))


def empty_literal_iterable(iterable: ast.expr) -> bool:
    if isinstance(iterable, ast.Dict):
        return not iterable.keys
    if isinstance(iterable, ast.List | ast.Set | ast.Tuple):
        return not iterable.elts
    return isinstance(iterable, ast.Constant) and iterable.value == ""


@final
class PythonKeyScan(ast.NodeVisitor):
    """Collect bindings and reads in one scope before resolving any reads."""

    def __init__(self, package: str, tokens: set[str], reads: SourceKeyReads,
                 outer: PythonKeyScan | None = None, *, class_scope: bool = False,
                 comprehension_scope: bool = False) -> None:
        self.package = package
        self.tokens = tokens
        self.reads = reads
        self.outer = outer
        self.class_scope = class_scope
        self.comprehension_scope = comprehension_scope
        self.local_names: set[str] = set()
        self.global_names: set[str] = set()
        self.nonlocal_names: set[str] = set()
        self.imports: ReadBindings = {}
        self.bindings: ReadBindings = {}
        self.assignments: list[tuple[str, ast.Call]] = []
        self.key_assignments: dict[str, list[KeyAssignment]] = {}
        self.loop_bodies: list[LoopBody] = []
        self.parameters: list[ast.arg] = []
        self.calls: list[FixedReadCall] = []
        self.children: list[PythonKeyScan] = []

    def child(self, *, class_scope: bool = False, comprehension_scope: bool = False) -> PythonKeyScan:
        scan = PythonKeyScan(self.package, self.tokens, self.reads, self, class_scope=class_scope,
                             comprehension_scope=comprehension_scope)
        self.children.append(scan)
        return scan

    def scopes(self) -> Iterator[PythonKeyScan]:
        yield self
        for child in self.children:
            yield from child.scopes()

    def enclosing(self) -> Iterator[PythonKeyScan]:
        scope = self.outer
        while scope is not None:
            if not scope.class_scope:
                yield scope
            scope = scope.outer

    def binding_scope(self, name: str) -> PythonKeyScan:
        if name in self.global_names:
            return next(scope for scope in (self, *self.enclosing()) if scope.outer is None)
        if name in self.nonlocal_names:
            for scope in self.enclosing():
                if (scope.outer is not None and name in scope.local_names
                        and name not in scope.global_names and name not in scope.nonlocal_names):
                    return scope
        return self

    def visible_bindings(self) -> ReadBindings:
        names: ReadBindings = {}
        for scope in (self, *self.enclosing()):
            for name, bindings in scope.bindings.items():
                names.setdefault(name, set()).update(bindings)
        return names

    @override
    def visit_Constant(self, node: ast.Constant) -> None:
        if self.tokens and isinstance(node.value, str) and node.value in self.tokens:
            self.reads.mentions.add((node.value, node.lineno))

    @override
    def visit_Import(self, node: ast.Import) -> None:
        for item in node.names:
            name = imported_name(item)
            self.local_names.add(name)
            if item.name == "scripts.lib.conf":
                kind = ReadName.CONFIG_MODULE if item.asname else ReadName.SCRIPTS_PACKAGE_WITH_CONF
                self.imports.setdefault(name, set()).add(kind)

    @override
    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if node.level:
            try:
                module = resolve_name("." * node.level + module, self.package)
            except ImportError:
                module = ""
        for item in node.names:
            name = imported_name(item)
            self.local_names.add(name)
            if module == "scripts.lib" and item.name == "conf":
                self.imports.setdefault(name, set()).add(ReadName.CONFIG_MODULE)
            elif module == "scripts.lib.conf":
                if item.name in _LOADERS:
                    self.imports.setdefault(name, set()).add(ReadName.CONFIG_LOADER)
                elif item.name == "ConfigRead":
                    self.imports.setdefault(name, set()).add(ReadName.CONFIG_READER_TYPE)

    @override
    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store | ast.Del):
            self.local_names.add(node.id)

    @override
    def visit_Global(self, node: ast.Global) -> None:
        self.global_names.update(node.names)

    @override
    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.nonlocal_names.update(node.names)

    def visit_named_binding(self, node: ast.ExceptHandler | ast.MatchAs | ast.MatchStar) -> None:
        if node.name is not None:
            self.local_names.add(node.name)
        self.generic_visit(node)

    visit_ExceptHandler = visit_MatchAs = visit_MatchStar = visit_named_binding

    @override
    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        if node.rest is not None:
            self.local_names.add(node.rest)
        self.generic_visit(node)

    def assign_key(self, target: ast.expr, assignment: KeyAssignment) -> None:
        if isinstance(target, ast.Name):
            if self.loop_bodies:
                loop = self.loop_bodies[-1]
                if loop.known_empty:
                    return
                assignment = LoopKeyAssignment(assignment.position, assignment.keys, loop.end)
            self.key_assignments.setdefault(target.id, []).append(assignment)
        elif isinstance(target, ast.Tuple | ast.List):
            for part in target.elts:
                self.assign_key(part, assignment)
        elif isinstance(target, ast.Starred):
            self.assign_key(target.value, assignment)

    @override
    def visit_Assign(self, node: ast.Assign | ast.AnnAssign | ast.AugAssign | ast.NamedExpr) -> None:
        scope = self
        if isinstance(node, ast.NamedExpr):
            while scope.comprehension_scope and scope.outer is not None:
                scope = scope.outer
            scope.visit(node.target)
        value = node.value
        if value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
            replaces_value = not isinstance(node, ast.AugAssign)
            keys = ((value.value,) if replaces_value and isinstance(value, ast.Constant)
                    and isinstance(value.value, str) else ())
            assignment = KeyAssignment(source_end(value), keys)
            for target in targets:
                scope.assign_key(target, assignment)
                if replaces_value and isinstance(target, ast.Name) and isinstance(value, ast.Call):
                    scope.assignments.append((target.id, value))
        if isinstance(node, ast.NamedExpr):
            self.visit(node.value)
        else:
            self.generic_visit(node)

    visit_AnnAssign = visit_AugAssign = visit_NamedExpr = visit_Assign

    def visit_loop(self, node: ast.For | ast.AsyncFor | ast.While) -> None:
        if isinstance(node, ast.While):
            self.visit(node.test)
            known_empty = isinstance(node.test, ast.Constant) and not node.test.value
        else:
            self.visit(node.iter)
            self.visit(node.target)
            known_empty = empty_literal_iterable(node.iter)
        known_empty = known_empty or bool(self.loop_bodies and self.loop_bodies[-1].known_empty)
        self.loop_bodies.append(LoopBody(source_end(node.body[-1]), known_empty))
        if not isinstance(node, ast.While):
            self.assign_key(node.target, KeyAssignment(source_end(node.iter), literal_loop_keys(node.iter)))
        for statement in node.body:
            self.visit(statement)
        _ = self.loop_bodies.pop()
        for statement in node.orelse:
            self.visit(statement)

    visit_For = visit_AsyncFor = visit_While = visit_loop

    def function_body(self, args: ast.arguments, body: Sequence[ast.AST]) -> None:
        self.visit(args)
        inner = self.child()
        inner.parameters = [arg for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg)
                            if arg is not None]
        inner.local_names.update(arg.arg for arg in inner.parameters)
        for statement in body:
            inner.visit(statement)

    @override
    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.local_names.add(node.name)
        for part in (*node.decorator_list, *node.type_params, node.returns):
            if part is not None:
                self.visit(part)
        self.function_body(node.args, node.body)

    visit_AsyncFunctionDef = visit_FunctionDef

    @override
    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.function_body(node.args, (node.body,))

    @override
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.local_names.add(node.name)
        for part in (*node.decorator_list, *node.type_params, *node.bases, *node.keywords):
            self.visit(part)
        inner = self.child(class_scope=True)
        for statement in node.body:
            inner.visit(statement)

    def visit_comprehension_read(self, node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp) -> None:
        first, *rest = node.generators
        self.visit(first.iter)
        inner = self.child(comprehension_scope=True)
        for generator in (first, *rest):
            if generator is not first:
                inner.visit(generator.iter)
            inner.assign_key(generator.target, KeyAssignment((0, 0), literal_loop_keys(generator.iter)))
            inner.visit(generator.target)
            for condition in generator.ifs:
                inner.visit(condition)
        if isinstance(node, ast.DictComp):
            inner.visit(node.key)
            inner.visit(node.value)
        else:
            inner.visit(node.elt)

    visit_ListComp = visit_SetComp = visit_DictComp = visit_GeneratorExp = visit_comprehension_read

    @override
    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute) and node.func.attr in _ACCESSORS:
            keys = node.args if node.func.attr == "values" else (
                node.args[:1] or [keyword.value for keyword in node.keywords if keyword.arg == "key"])
            if keys:
                self.calls.append(FixedReadCall(node.func.value, keys))
        self.generic_visit(node)

    def bind_imports(self) -> None:
        for name, bindings in self.imports.items():
            self.binding_scope(name).bindings.setdefault(name, set()).update(bindings)

    def bind_readers(self) -> None:
        annotations = self.outer.visible_bindings() if self.outer is not None and self.parameters else {}
        for parameter in self.parameters:
            if config_reader_parameter(parameter.annotation, annotations):
                self.bindings.setdefault(parameter.arg, set()).add(ReadName.CONFIG_READER)
        if self.assignments:
            names = self.visible_bindings()
            for name, call in self.assignments:
                if is_config_loader(call.func, names):
                    self.binding_scope(name).bindings.setdefault(name, set()).add(ReadName.CONFIG_READER)

    def assigned_keys(self, key: ast.Name) -> set[str]:
        scope = self
        while scope.comprehension_scope and key.id not in scope.local_names and scope.outer is not None:
            scope = scope.outer
        position = key.lineno, key.col_offset
        keys: set[str] = set()
        for assignment in sorted(scope.key_assignments.get(key.id, ()), key=lambda item: item.position, reverse=True):
            if assignment.position <= position:
                keys.update(assignment.keys)
                # A completed loop can leave the earlier value in place.
                if not isinstance(assignment, LoopKeyAssignment) or position < assignment.body_end:
                    break
        return keys

    def resolve_calls(self) -> None:
        if not self.calls:
            return
        names = self.visible_bindings()
        if not names:
            return
        for call in self.calls:
            receiver = call.receiver
            if (isinstance(receiver, ast.Call) and is_config_loader(receiver.func, names)
                    or isinstance(receiver, ast.Name) and ReadName.CONFIG_READER in names.get(receiver.id, ())):
                for key in call.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        self.reads.fixed.add((key.value, key.lineno))
                    elif isinstance(key, ast.Name):
                        self.reads.fixed.update((value, key.lineno) for value in self.assigned_keys(key))


def python_key_reads(source: str, content: bytes, tokens: set[str]) -> SourceKeyReads:
    reads = SourceKeyReads()
    package = ".".join(PurePosixPath(source).parts[:-1])
    scan = PythonKeyScan(package, tokens, reads)
    scan.visit(ast.parse(content, source))
    scopes = tuple(scan.scopes())
    for scope in scopes:
        scope.bind_imports()
    for scope in scopes:
        scope.bind_readers()
    for scope in scopes:
        scope.resolve_calls()
    return reads


_SHELL_REDIRECT = r"<<<|<<-?|<>|<&|&>>?|>>|>\||>&|[<>]"
_SHELL_PART = re.compile(rf"""\\(?:\r?\n|[^\n])|\$\(|[ \t\r]+|(?P<redirect>{_SHELL_REDIRECT})|[^\\$#;|&()<>"' \x60\t\r\n]+|[\s\S]""")
_SHELL_KEY = re.compile(r"[\w./-]+")
_SHELL_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_SHELL_CONTROL = {"if", "then", "elif", "else", "while", "until", "do", "!"}
_SHELL_BOUNDARY = {";", "&", "|", "(", ")", "\n"}
_SHELL_REDIRECT_WORD = re.compile(_SHELL_REDIRECT)


def shell_commands(text: str) -> Iterator[list[tuple[str, int]]]:
    """Collect shell words and retain the physical line of each word."""
    offset, line = 0, 1

    def commands(end: str = "") -> Iterator[list[tuple[str, int]]]:
        nonlocal offset, line
        words: list[tuple[str, int]] = []
        word, quote, word_line = "", "", line
        while offset < len(text):
            match = _SHELL_PART.match(text, offset)
            assert match is not None
            part = match[0]
            offset = match.end()
            part_line = line
            line += part.count("\n")
            if part in ("\\\n", "\\\r\n"):
                continue
            if not word:
                word_line = part_line
            if part == "#" and not quote and not word:
                newline = text.find("\n", offset)
                offset = len(text) if newline == -1 else newline
                continue
            if part == end and not quote:
                break
            if part in ("$(", "\x60"):
                word += part
                yield from commands(")" if part == "$(" else part)
                word += ")" if part == "$(" else part
                continue
            if part == "'" and not quote:
                closing = text.find("'", offset)
                closing = len(text) if closing == -1 else closing + 1
                literal = text[offset:closing]
                word += part + literal
                line += literal.count("\n")
                offset = closing
                continue
            if part == '"':
                quote = "" if quote else part
            redirect = match["redirect"] is not None
            if quote or (part not in _SHELL_BOUNDARY and not redirect and not part.isspace()):
                word += part
                continue
            if word and not (redirect and word.isascii() and word.isdigit()):
                words.append((word, word_line))
            word = ""
            if words and part in _SHELL_BOUNDARY:
                yield words
                words = []
            if redirect:
                words.append((part, part_line))
        if word:
            words.append((word, word_line))
        if words:
            yield words

    yield from commands()


def shell_literal(word: str) -> str:
    return word[1:-1] if len(word) > 1 and word[0] == word[-1] and word[0] in "\"'" else word


def shell_command_start(words: Sequence[tuple[str, int]]) -> int:
    has_prefix = False
    prefix = iter(enumerate(words))
    for index, (word, _) in prefix:
        if _SHELL_REDIRECT_WORD.fullmatch(word):
            _ = next(prefix, None)
            has_prefix = True
        elif _SHELL_ASSIGNMENT.match(word):
            has_prefix = True
        elif has_prefix or word not in _SHELL_CONTROL:
            return index
    return len(words)


def shell_key_reads(text: str, mentions: re.Pattern[str] | None) -> SourceKeyReads:
    reads = SourceKeyReads()
    for words in shell_commands(text):
        if mentions is not None:
            for word, line in words:
                reads.mentions.update((match[0], line + word.count("\n", 0, match.start()))
                                      for match in mentions.finditer(word))
        first = shell_command_start(words)
        command = words[first:first + 5]
        if len(command) < 3:
            continue
        program = shell_literal(command[0][0])
        if program in ("conf_value", "conf_get"):
            key, line = command[2]
        else:
            executable = program.rsplit("/", 1)[-1]
            cli = command[1:] if executable == "py" or executable.startswith("python") else command
            if (len(cli) < 4 or shell_literal(cli[0][0]).rsplit("/", 1)[-1] != "conf.py"
                    or shell_literal(cli[1][0]) != "get"):
                continue
            key, line = cli[3]
        key = shell_literal(key)
        if _SHELL_KEY.fullmatch(key):
            reads.fixed.add((key, line))
    return reads


def tip_key_reads(repo: Path, tip_files: dict[str, str], removed: dict[str, set[str]],
                  tip_keys: dict[str, set[str]]) -> AdvisoryReads:
    removed_from: dict[str, list[str]] = {}
    for name, keys in sorted(removed.items()):
        for key in keys:
            removed_from.setdefault(key, []).append(name)

    held = {key for keys in tip_keys.values() for key in keys}
    shared = removed_from.keys() & held
    # A dotted key also holds each prefix before a dot.
    held.update(key[:index] for key in tuple(held) for index, char in enumerate(key) if char == ".")
    aliases = {name: {name, PurePosixPath(name).stem} for name in removed}
    tokens = set(removed_from) | {alias for names in aliases.values() for alias in names}
    alternatives = "|".join(map(re.escape, sorted(tokens, key=lambda token: (-len(token), token))))
    word = r"[A-Za-z0-9_.-]"
    shell_mentions = re.compile(rf"(?<!{word})(?:{alternatives})(?!{word})") if tokens else None

    sources = {path: blob for path, blob in tip_files.items() if path.startswith("scripts/") and (
        path.endswith((".sh", ".zsh", ".bash")) or path.endswith(".py") and not is_fixture_source(path))}
    advisory = AdvisoryReads(sources_not_scanned=len(sources))
    if not sources:
        return advisory
    try:
        for source, content in blob_contents(repo, sources, report_missing=True):
            if isinstance(content, MissingBlob):
                continue
            try:
                reads = (python_key_reads(source, content, tokens) if source.endswith(".py")
                         else shell_key_reads(content.decode("utf-8"), shell_mentions))
            except (SyntaxError, UnicodeError, RecursionError, ValueError):
                continue
            advisory.sources_not_scanned -= 1
            named_tokens = {token for token, _ in reads.mentions}
            reported: set[KeyOccurrence] = set()
            for key, line in reads.mentions:
                names = tuple(name for name in removed_from.get(key, ())
                              if key not in shared or not aliases[name].isdisjoint(named_tokens))
                if names:
                    advisory.removed.append(RemovedKeyRead(source, line, key, names))
                    reported.add((key, line))
            advisory.absent.extend(AbsentKeyRead(source, line, key) for key, line in reads.fixed - reported
                                   if key not in held)
    except (GitReadFailed, OSError):
        pass
    return advisory


def check(repo: Path, base: str, tip: str) -> KeyReport:
    base_files, tip_files = regular_files(repo, base), regular_files(repo, tip)

    def confs(files: dict[str, str]) -> dict[str, str]:
        return {path.removeprefix("config/"): blob for path, blob in files.items()
                if path.startswith("config/") and path.endswith(".conf")}

    base_confs, tip_confs = confs(base_files), confs(tip_files)
    texts = blob_texts(repo, set(base_confs.values()) | set(tip_confs.values()))
    added = new_keys({PurePosixPath(name).name: texts[blob] for name, blob in base_confs.items()},
                     {PurePosixPath(name).name: texts[blob] for name, blob in tip_confs.items()})
    tip_keys = {name: set(conf.settings(texts[blob])) for name, blob in tip_confs.items()}
    removed: dict[str, set[str]] = {}
    for name, blob in base_confs.items():
        if "/" not in name and blob != tip_confs.get(name):
            keys = set(conf.settings(texts[blob])) - tip_keys.get(name, set())
            if keys:
                removed[name] = keys
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
    advisory = tip_key_reads(repo, tip_files, removed, tip_keys)
    return KeyReport(added, missing, not_checked, removed, advisory)


def report(result: KeyReport) -> int:
    for name, keys in sorted(result.new_keys.items()):
        print(f"new keys in config/{name}: {', '.join(sorted(keys))}")
    for gap in result.missing:
        print(f"{gap.write.source}:{gap.write.line}: the config/{gap.write.conf_name} fixture lacks {gap.key}")
    for name, keys in sorted(result.removed_keys.items()):
        print(f"removed keys in config/{name}: {', '.join(sorted(keys))}")
    advisory = result.advisory
    reads: list[RemovedKeyRead | AbsentKeyRead] = [*advisory.removed, *advisory.absent]
    for read in sorted(reads, key=lambda read: (read.source, read.line, read.key)):
        reason = ("the tip removes from " + ", ".join(f"config/{name}" for name in read.conf_names)
                  if isinstance(read, RemovedKeyRead)
                  else "no config file on the tip holds")
        print(f"{read.source}:{read.line}: reads {read.key}, which {reason}")
    print(f"fixture config keys: {len(result.missing)} missing, "
          + f"{len(result.not_checked)} fixtures not checked because their text is not a literal")
    summary = (f"config key reads: {len(advisory.removed)} of a removed key, "
               + f"{len(advisory.absent)} of a key no config file holds")
    if advisory.sources_not_scanned:
        summary += f", {advisory.sources_not_scanned} sources not scanned"
    print(summary)
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
