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
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from enum import Enum, auto
from io import BytesIO
from pathlib import Path, PurePosixPath
import re
import sys
from typing import final, override

from scripts.lib import conf
from scripts.production.affected_modules import GraphError, blob_texts, git_output, scope_nodes
from scripts.production.gate_modules import is_test_module
from scripts.style.git_process import GitBlob, GitReadFailed, parse_tree_listing, read_batch_blob, run_git


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
    UNRESOLVED = auto()


type ReadBinding = ReadName | str
type ReadBindings = dict[str, ReadBinding]
type KeyOccurrence = tuple[str, int]


@dataclass
class SourceKeyReads:
    mentions: set[KeyOccurrence] = field(default_factory=set)
    fixed: set[KeyOccurrence] = field(default_factory=set)


def imported_name(item: ast.alias) -> str:
    return item.asname or item.name.partition(".")[0]


def is_config_module(node: ast.expr, names: ReadBindings) -> bool:
    if isinstance(node, ast.Name):
        return names.get(node.id) is ReadName.CONFIG_MODULE
    return (isinstance(node, ast.Attribute) and node.attr == "conf"
            and isinstance(node.value, ast.Attribute) and node.value.attr == "lib"
            and isinstance(node.value.value, ast.Name)
            and names.get(node.value.value.id) is ReadName.SCRIPTS_PACKAGE_WITH_CONF)


def is_config_loader(node: ast.expr, names: ReadBindings) -> bool:
    if isinstance(node, ast.Name):
        return names.get(node.id) is ReadName.CONFIG_LOADER
    return (isinstance(node, ast.Attribute) and node.attr in _LOADERS
            and is_config_module(node.value, names))


def config_reader_parameter(annotation: ast.expr | None, names: ReadBindings) -> bool:
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            annotation = ast.parse(annotation.value, mode="eval").body
        except SyntaxError:
            return False
    if isinstance(annotation, ast.Name):
        return names.get(annotation.id) is ReadName.CONFIG_READER_TYPE
    return (isinstance(annotation, ast.Attribute) and annotation.attr == "ConfigRead"
            and is_config_module(annotation.value, names))


def function_locals(nodes: Iterable[ast.AST]) -> Iterator[str]:
    """Find local names that hide enclosing bindings before their first assignment."""
    for node in nodes:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            yield node.name
        elif isinstance(node, ast.Import | ast.ImportFrom):
            yield from map(imported_name, node.names)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del):
            yield node.id
        elif not isinstance(node, ast.Lambda | ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp):
            yield from function_locals(ast.iter_child_nodes(node))


@final
class PythonKeyScan(ast.NodeVisitor):
    """Collect key mentions and resolve fixed reads in statement order."""

    def __init__(self, tokens: set[str], reads: SourceKeyReads, names: ReadBindings,
                 closure_names: ReadBindings) -> None:
        self.tokens = tokens
        self.reads = reads
        self.names = names
        self.closure_names = closure_names

    @override
    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and node.value in self.tokens:
            self.reads.mentions.add((node.value, node.lineno))

    @override
    def visit_Import(self, node: ast.Import) -> None:
        for item in node.names:
            name = imported_name(item)
            if item.name == "scripts.lib.conf":
                self.names[name] = (ReadName.CONFIG_MODULE if item.asname
                                    else ReadName.SCRIPTS_PACKAGE_WITH_CONF)
            elif item.asname or name != "scripts" or self.names.get(name) is not ReadName.SCRIPTS_PACKAGE_WITH_CONF:
                self.names[name] = ReadName.UNRESOLVED

    @override
    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for item in node.names:
            binding = ReadName.UNRESOLVED
            if node.level == 0:
                if node.module == "scripts.lib" and item.name == "conf":
                    binding = ReadName.CONFIG_MODULE
                elif node.module == "scripts.lib.conf":
                    if item.name in _LOADERS:
                        binding = ReadName.CONFIG_LOADER
                    elif item.name == "ConfigRead":
                        binding = ReadName.CONFIG_READER_TYPE
            self.names[imported_name(item)] = binding

    @override
    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store | ast.Del):
            self.names[node.id] = ReadName.UNRESOLVED

    def assign(self, targets: Sequence[ast.expr], value: ast.expr) -> None:
        self.visit(value)
        binding: ReadBinding = ReadName.UNRESOLVED
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            binding = value.value
        elif isinstance(value, ast.Call) and is_config_loader(value.func, self.names):
            binding = ReadName.CONFIG_READER
        for target in targets:
            self.visit(target)
            if isinstance(target, ast.Name):
                self.names[target.id] = binding

    @override
    def visit_Assign(self, node: ast.Assign) -> None:
        self.assign(node.targets, node.value)

    @override
    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.visit(node.annotation)
        if node.value is not None:
            self.assign((node.target,), node.value)

    @override
    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.assign((node.target,), node.value)

    @override
    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.visit(node.value)
        self.visit(node.target)

    @override
    def visit_For(self, node: ast.For | ast.AsyncFor) -> None:
        self.visit(node.iter)
        self.visit(node.target)
        for statement in (*node.body, *node.orelse):
            self.visit(statement)

    visit_AsyncFor = visit_For

    def function_scope(self, args: ast.arguments, body: Sequence[ast.AST]) -> PythonKeyScan:
        names = self.closure_names | dict.fromkeys(function_locals(body), ReadName.UNRESOLVED)
        for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg):
            if arg is not None:
                names[arg.arg] = (ReadName.CONFIG_READER if config_reader_parameter(arg.annotation, self.names)
                                  else ReadName.UNRESOLVED)
        return PythonKeyScan(self.tokens, self.reads, names, names)

    @override
    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for part in (*node.decorator_list, *node.type_params, node.args, node.returns):
            if part is not None:
                self.visit(part)
        inner = self.function_scope(node.args, node.body)
        for statement in node.body:
            inner.visit(statement)
        self.names[node.name] = ReadName.UNRESOLVED

    visit_AsyncFunctionDef = visit_FunctionDef

    @override
    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.visit(node.args)
        self.function_scope(node.args, (node.body,)).visit(node.body)

    @override
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for part in (*node.decorator_list, *node.type_params, *node.bases, *node.keywords):
            self.visit(part)
        inner = PythonKeyScan(self.tokens, self.reads, self.names.copy(), self.closure_names)
        for statement in node.body:
            inner.visit(statement)
        self.names[node.name] = ReadName.UNRESOLVED

    def visit_comprehension_read(self, node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp) -> None:
        names = self.names.copy()
        inner = PythonKeyScan(self.tokens, self.reads, names, names)
        for generator in node.generators:
            inner.visit(generator.iter)
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
            receiver = node.func.value
            if (isinstance(receiver, ast.Call) and is_config_loader(receiver.func, self.names)
                    or isinstance(receiver, ast.Name) and self.names.get(receiver.id) is ReadName.CONFIG_READER):
                arguments = node.args if node.func.attr == "values" else (
                    node.args[:1] or [keyword.value for keyword in node.keywords if keyword.arg == "key"])
                for arg in arguments:
                    value = arg.value if isinstance(arg, ast.Constant) else (
                        self.names.get(arg.id) if isinstance(arg, ast.Name) else None)
                    if isinstance(value, str):
                        self.reads.fixed.add((value, arg.lineno))
        self.generic_visit(node)


def python_key_reads(source: str, content: bytes, tokens: set[str]) -> SourceKeyReads:
    reads = SourceKeyReads()
    names: ReadBindings = {}
    PythonKeyScan(tokens, reads, names, names).visit(ast.parse(content, source))
    return reads


_SHELL_PART = re.compile(r"""\\(?:\r?\n|[^\n])|\$\(|[ \t\r]+|[^\\$#;|&()<>"' \x60\t\r\n]+|[\s\S]""")
_SHELL_KEY = re.compile(r"[\w./-]+")
_SHELL_CONTROL = {"if", "then", "elif", "else", "while", "until", "do", "!"}
_SHELL_BOUNDARY = {";", "&", "|", "(", ")", "\n"}
_SHELL_REDIRECT = {"<", ">"}


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
            if quote or (part not in _SHELL_BOUNDARY and part not in _SHELL_REDIRECT and not part.isspace()):
                word += part
                continue
            if word:
                words.append((word, word_line))
                word = ""
            if words and part in _SHELL_BOUNDARY:
                yield words
                words = []
            if part in _SHELL_REDIRECT:
                words.append((part, part_line))
        if word:
            words.append((word, word_line))
        if words:
            yield words

    yield from commands()


def shell_literal(word: str) -> str:
    return word[1:-1] if len(word) > 1 and word[0] == word[-1] and word[0] in "\"'" else word


def shell_key_reads(text: str, mentions: re.Pattern[str] | None) -> SourceKeyReads:
    reads = SourceKeyReads()
    for words in shell_commands(text):
        if mentions is not None:
            for word, line in words:
                reads.mentions.update((match[0], line + word.count("\n", 0, match.start()))
                                      for match in mentions.finditer(word))
        first = next((index for index, (word, _) in enumerate(words) if word not in _SHELL_CONTROL), len(words))
        command = words[first:first + 4]
        if len(command) < 3:
            continue
        program = shell_literal(command[0][0])
        if program in ("conf_value", "conf_get"):
            key, line = command[2]
        elif (program.rsplit("/", 1)[-1] == "conf.py" and len(command) == 4
              and shell_literal(command[1][0]) == "get"):
            key, line = command[3]
        else:
            continue
        key = shell_literal(key)
        if _SHELL_KEY.fullmatch(key):
            reads.fixed.add((key, line))
    return reads


def advisory_source_contents(repo: Path, sources: dict[str, str]) -> Iterator[tuple[str, bytes]]:
    """Yield source bytes from the batch replies that Git can read."""
    command = ("git", "-C", str(repo), "cat-file", "--batch")
    replies = BytesIO(run_git(command, input="".join(f"{blob}\n" for blob in sources.values()).encode()).stdout)
    for source, blob in sources.items():
        reply = read_batch_blob(replies)
        if isinstance(reply, GitBlob) and reply.object_id == blob:
            yield source, reply.content


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
        for source, content in advisory_source_contents(repo, sources):
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
    except GitReadFailed:
        return advisory
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
