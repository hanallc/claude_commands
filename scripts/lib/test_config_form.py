"""Check config files and the code that uses them."""

import ast
from collections.abc import Iterator, Sequence
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from pathlib import Path
import re
import subprocess
import tempfile
from typing import NamedTuple, cast
import unittest

from scripts.lib import conf, config_form_exceptions_test_support as exceptions
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
    violations = [Violation(path, 0, "format", path.name) for path in files if path.suffix != ".conf"]
    for path, lines in _conf_lines(root, files):
        for number, line in enumerate(lines, start=1):
            if _without_comment(line) and not _is_section(line):
                row = _key_row(line)
                if row is None or not row.key:
                    violations.append(Violation(path, number, "format", line))
    return violations


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


_SHELL_SUFFIXES = {".sh", ".bash", ".zsh"}
_SHELL_SPECIAL = re.compile(r"""['"\\#]""")
_SHELL_VARIABLE = re.compile(r"(?<!\\)\$(?:\{[!#]?)?([a-zA-Z_][a-zA-Z0-9_]*)")
_SHELL_ASSIGNMENT = re.compile(r"(?:^|\s)([a-zA-Z_][a-zA-Z0-9_]*)=")
_SHELL_OPERAND = re.compile(r"""(?:'[^']*'|"(?:\\.|[^"\\])*"|[^\s;'"<>&])+""")
_SHELL_READ = re.compile(
    r"(?<![\w./-])(?:awk|cat|cut|grep|head|mapfile|rg|sed|source|tail)\s|(?<![<:\w-])<(?=[\s\"'$/.]|config/)"
)
# Consume other words whole so quoted text cannot start a file read.
_SHELL_JQ_FILE_READ = re.compile(
    r"""(?P<quote>['"]?)--(?:raw|slurp)file(?P=quote)"""
    + rf"\s+{_SHELL_OPERAND.pattern}\s+(?P<path>{_SHELL_OPERAND.pattern})"
    + r"""|(?:'[^']*(?:'|$)|"(?:\\.|[^"\\])*(?:"|$)|\\.|[^\s;'"<>&|()\\])+"""
)
_SHELL_HEREDOC = re.compile(r"""(?<!<)<<-?\s*(?P<quote>['"]?)(?P<end>[A-Za-z_][A-Za-z0-9_]*)(?P=quote)""")
_CONFIG_PATH = re.compile(r"(?:^|[/:'\"=\s])config/([\w.*-]+(?:/[\w.*-]+)*)?")
_CONFIG_FILENAME = re.compile(r"[\w.-]+\.(?:conf|toml)")
_CONFIG_PATH_HINT = r"""\bconfig/|["']config["']|/config\b|\.conf(?=["'}/\s]|$)"""
_PYTHON_CONFIG_HINT = (
    r"\bconf_reads\b|\bconf\s*(?:\.\s*(?:path|directory|read_bytes)\b|as\b|import\b)"
)
_RAW_PYTHON_HINT = re.compile(
    r"\b(?:read_text|read_bytes|read_file|read_string|tomllib|configparser)\b"
    + r"|\bopen\s*\((?!__file__\[[^\n]*package_root\.py)"
    + r"|\bfrom\s+(?:io|os|builtins)\s+import\s+(?:\([^)]*|[^\n]*)\bopen\b"
)
_ENVIRONMENT_HINT = re.compile(r"\b(?:environ|getenv)\b")
_CONFIG_VARIABLE_HINT = re.compile(r"""_CONFIG(?:_FILE)?(?=["'])""")
_SHARED_TEXT_READER = Path("scripts/lib/conf_reads.py")
_MODEL_READERS = {
    Path("scripts/lib/models_registry.py"), Path("scripts/lib/registry_rows.py"),
    Path("scripts/agents/models_config.sh"), Path("scripts/agents/sync_codex_catalog.sh"),
}


class _SourceFile(NamedTuple):
    path: Path
    text: str
    lines: list[str]

    def violation(self, number: int, rule: str) -> Violation:
        return Violation(self.path, number, rule, self.lines[number - 1])


def _source_files(root: Path, files: Sequence[Path], suffixes: set[str]) -> Iterator[_SourceFile]:
    for path in files:
        if (path.name.startswith("test_") or path.name.endswith(("_test_support.py", "_fixture.py"))
                or {"fixtures", "tests", "testdata", "test_data"}.intersection(path.parts)):
            continue
        if path.suffix and path.suffix not in suffixes:
            continue
        source = root / path
        if not path.suffix and not source.is_file():
            continue
        text = source.read_text(encoding="utf-8")
        lines = text.split("\n")
        if not path.suffix:
            shebang = lines[0] if lines and lines[0].startswith("#!") else ""
            if not ((".py" in suffixes and re.search(r"\bpython[\d.]*\b", shebang))
                    or (_SHELL_SUFFIXES.intersection(suffixes) and re.search(r"\b(?:ba|z)?sh\b", shebang))):
                continue
        yield _SourceFile(path, text, lines)


def _python_source(source: _SourceFile) -> bool:
    return source.path.suffix == ".py" or (
        not source.path.suffix and bool(re.search(r"\bpython[\d.]*\b", source.lines[0]))
    )


def _string(node: ast.AST) -> str:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else ""


def _dotted_name(node: ast.AST, imports: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return imports.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        return f"{_dotted_name(node.value, imports)}.{node.attr}"
    return ""


def _record_import(node: ast.Import | ast.ImportFrom, names: dict[str, str]) -> None:
    if isinstance(node, ast.Import):
        for alias in node.names:
            first = alias.name.partition(".")[0]
            names[alias.asname or first] = alias.name if alias.asname else first
    elif node.module:
        for alias in node.names:
            names[alias.asname or alias.name] = f"{node.module}.{alias.name}"


def _assignment_pairs(target: ast.expr, value: ast.expr) -> Iterator[tuple[ast.Name, ast.expr]]:
    if isinstance(target, ast.Name):
        yield target, value
    elif (isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List))
          and len(target.elts) == len(value.elts) and not any(isinstance(item, ast.Starred) for item in value.elts)):
        for part, element in zip(target.elts, value.elts, strict=True):
            yield from _assignment_pairs(part, element)


def _assignments(node: ast.AST) -> Iterator[tuple[ast.Name, ast.expr]]:
    if isinstance(node, ast.Assign):
        for target in node.targets:
            yield from _assignment_pairs(target, node.value)
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
        yield node.target, node.value


def _scope_nodes(body: Sequence[ast.AST]) -> Iterator[ast.AST]:
    pending = list(reversed(body))
    while pending:
        node = pending.pop()
        node_type = type(node)
        if node_type in _SCOPE_LEAVES or not node._fields:
            continue
        if node_type in _SCOPE_FACTS:
            yield node
        if node_type in _SCOPE_DEFINITIONS:
            pending.extend(reversed(_definition_expressions(node)))
        else:
            for field in reversed(node._fields):
                child = cast(object, getattr(node, field))
                if isinstance(child, ast.AST):
                    pending.append(child)
                elif isinstance(child, list):
                    pending.extend(item for item in reversed(cast(list[object], child)) if isinstance(item, ast.AST))


_SCOPE_LEAVES = {ast.Name, ast.Constant, ast.alias}
_SCOPE_DEFINITIONS = {ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda}
_SCOPE_FACTS = _SCOPE_DEFINITIONS | {
    ast.Assign, ast.AnnAssign, ast.Return, ast.Call, ast.Compare, ast.Subscript, ast.Import, ast.ImportFrom,
}


def _definition_expressions(node: ast.AST) -> list[ast.AST]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return [*node.decorator_list, *(value for _, value in _parameter_defaults(node.args))]
    if isinstance(node, ast.Lambda):
        return [value for _, value in _parameter_defaults(node.args)]
    if isinstance(node, ast.ClassDef):
        return [*node.decorator_list, *node.bases, *node.keywords]
    return []


type _ScopeDefinition = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda


class _PythonScope(NamedTuple):
    bindings: list[tuple[ast.Name, ast.expr]]
    returns: list[ast.Return]
    reads: list[ast.Call | ast.Compare | ast.Subscript]
    children: list[tuple[_ScopeDefinition, "_PythonScope"]]


def _python_scope(body: Sequence[ast.AST], imports: dict[str, str]) -> _PythonScope:
    scope = _PythonScope([], [], [], [])
    for node in _scope_nodes(body):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            child_body = [node.body] if isinstance(node, ast.Lambda) else node.body
            scope.children.append((node, _python_scope(child_body, imports)))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            scope.bindings.extend(_assignments(node))
        elif isinstance(node, ast.Return):
            scope.returns.append(node)
        elif isinstance(node, (ast.Call, ast.Compare, ast.Subscript)):
            scope.reads.append(node)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            _record_import(node, imports)
    return scope


_NO_CONFIG_FILES: frozenset[str] = frozenset()
_UNKNOWN_CONFIG_FILE = frozenset({"*"})


def _config_path_files(value: str, known_filenames: set[str]) -> frozenset[str]:
    if "config/" in value and (match := _CONFIG_PATH.search(value)):
        filename = match[1].rsplit("/", 1)[-1] if match[1] else ""
        if "*" in filename:
            return _UNKNOWN_CONFIG_FILE
        if filename in known_filenames or filename.endswith(".conf"):
            return frozenset({filename})
        if value.startswith(("config/", "~/.claude/config/")) or (
            not value.startswith(".claude/config/")
            and (not filename or filename.endswith((".toml", ".json", ".txt", ".md")))
        ):
            if filename and "." in filename and "*" not in filename and value[match.end():match.end() + 1] != "/":
                return frozenset({filename})
            return _UNKNOWN_CONFIG_FILE
    if value in known_filenames or value.endswith(".conf") and _CONFIG_FILENAME.fullmatch(value):
        return frozenset({value})
    if value == ".conf" or value.endswith("/.claude/config"):
        return _UNKNOWN_CONFIG_FILE
    return _NO_CONFIG_FILES


def _call_argument(call: ast.Call, keyword: str) -> ast.expr | None:
    if call.args:
        return call.args[0]
    return next((item.value for item in call.keywords if item.arg == keyword), None)


class _PythonConfigPaths(NamedTuple):
    imports: dict[str, str]
    known_filenames: set[str]

    def files(self, node: ast.AST | None, names: dict[str, frozenset[str]]) -> frozenset[str]:
        if node is None:
            return _NO_CONFIG_FILES
        if isinstance(node, ast.Name):
            return names.get(node.id, _NO_CONFIG_FILES)
        if isinstance(node, ast.Constant):
            return _config_path_files(node.value, self.known_filenames) if isinstance(node.value, str) else _NO_CONFIG_FILES
        if isinstance(node, ast.Call):
            method = _dotted_name(node.func, self.imports) if (
                isinstance(node.func, ast.Name)
                or isinstance(node.func, ast.Attribute) and node.func.attr in {"path", "directory"}
            ) else ""
            if method in {"conf.directory", "scripts.lib.conf.directory"}:
                return _UNKNOWN_CONFIG_FILE
            if method in {"conf.path", "scripts.lib.conf.path"}:
                argument = _call_argument(node, "file")
                if argument is not None and (name := _string(argument)):
                    return frozenset({Path(name if name.endswith(".conf") else f"{name}.conf").name})
                return self.files(argument, names) or _UNKNOWN_CONFIG_FILE
            if method in {"Path", "pathlib.Path", "PosixPath", "pathlib.PosixPath"}:
                argument = _call_argument(node, "path")
                if isinstance(argument, ast.Constant) and argument.value == "config":
                    return _UNKNOWN_CONFIG_FILE
            if isinstance(node.func, ast.Attribute) and node.func.attr in {"with_name", "with_suffix"}:
                return self._changed_path(node, node.func, names)
        if isinstance(node, ast.JoinedStr):
            text = "".join(_string(part) if isinstance(part, ast.Constant) else "*" for part in node.values)
            return _config_path_files(text, self.known_filenames) or self._child_files(node, names)
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Div, ast.Add)):
            left = self.files(node.left, names)
            right = self.files(node.right, names)
            if isinstance(node.op, ast.Div) and _string(node.right) == "config":
                return _UNKNOWN_CONFIG_FILE
            if left and isinstance(node.op, ast.Div) and (value := _string(node.right)):
                filename = value.rsplit("/", 1)[-1]
                if filename and ("." in filename or filename in self.known_filenames):
                    return frozenset({filename})
            return right or (_UNKNOWN_CONFIG_FILE if left and isinstance(node.op, ast.Div) else left)
        return self._child_files(node, names)

    def _child_files(self, node: ast.AST, names: dict[str, frozenset[str]]) -> frozenset[str]:
        files = _NO_CONFIG_FILES
        for child in ast.iter_child_nodes(node):
            if child._fields:
                child_files = self.files(child, names)
                if child_files:
                    files = files | child_files if files else child_files
        return files

    def _changed_path(self, call: ast.Call, function: ast.Attribute,
                      names: dict[str, frozenset[str]]) -> frozenset[str]:
        base = self.files(function.value, names)
        argument = _call_argument(call, "name" if function.attr == "with_name" else "suffix")
        value = _string(argument) if argument is not None else ""
        if function.attr == "with_name":
            return (frozenset({Path(value).name}) if value else _UNKNOWN_CONFIG_FILE) if base else (
                self.files(argument, names)
            )
        if not value or not base:
            return base
        if value not in {".conf", ".toml"}:
            return frozenset()
        return frozenset(str(Path(name).with_suffix(value)) if name != "*" else name for name in base)


def _config_names(scope: _PythonScope, inherited: dict[str, frozenset[str]],
                  paths: _PythonConfigPaths) -> dict[str, frozenset[str]]:
    names = inherited.copy()
    for target, value in scope.bindings:
        if files := paths.files(value, names):
            names[target.id] = names.get(target.id, frozenset()) | files
    for node, child in scope.children:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            files = frozenset(name for statement in child.returns
                              for name in paths.files(statement.value, names))
            if files:
                names[node.name] = files
    return names


def _parameter_defaults(parameters: ast.arguments) -> Iterator[tuple[ast.arg, ast.expr]]:
    positional = [*parameters.posonlyargs, *parameters.args]
    yield from zip(positional[len(positional) - len(parameters.defaults):], parameters.defaults, strict=True)
    yield from ((argument, value) for argument, value in zip(parameters.kwonlyargs, parameters.kw_defaults, strict=True)
                if value is not None)


def _opens_for_reading(call: ast.Call, mode_index: int) -> bool:
    if len(call.args) > mode_index:
        mode = _string(call.args[mode_index])
    else:
        mode = next((_string(keyword.value) for keyword in call.keywords if keyword.arg == "mode"), "")
    return not mode.startswith(("w", "a", "x")) or "+" in mode


def _config_parser(node: ast.AST, imports: dict[str, str]) -> bool:
    return isinstance(node, ast.Call) and _dotted_name(node.func, imports) in {
        "configparser.ConfigParser", "configparser.RawConfigParser", "configparser.SafeConfigParser",
    }


def _raw_python_read(call: ast.Call, names: dict[str, frozenset[str]], parsers: set[str],
                     paths: _PythonConfigPaths) -> frozenset[str]:
    if isinstance(call.func, ast.Attribute) and call.func.attr not in _RAW_READER_ATTRIBUTES:
        return _NO_CONFIG_FILES
    method = _dotted_name(call.func, paths.imports)
    if method in {"conf_reads.read_text", "scripts.lib.conf_reads.read_text"}:
        return paths.files(_call_argument(call, "path"), names) or _UNKNOWN_CONFIG_FILE
    if method in {"conf.read", "conf.read_or_repository", "scripts.lib.conf.read", "scripts.lib.conf.read_or_repository"}:
        return frozenset()
    if method in {"open", "io.open", "builtins.open"}:
        return paths.files(_call_argument(call, "file"), names) if _opens_for_reading(call, 1) else frozenset()
    if method in {"os.open", "tomllib.load", "tomllib.loads", "conf.read_bytes", "scripts.lib.conf.read_bytes"}:
        return paths.files(_call_argument(call, "path"), names)
    if isinstance(call.func, ast.Attribute):
        receiver = call.func.value
        if call.func.attr in {"open", "read_bytes", "read_text"} and (
            call.func.attr != "open" or _opens_for_reading(call, 0)
        ):
            return paths.files(receiver, names)
        if call.func.attr in {"read", "read_file", "read_string"} and (
            _config_parser(receiver, paths.imports) or (isinstance(receiver, ast.Name) and receiver.id in parsers)
        ):
            return paths.files(_call_argument(call, "filenames"), names)
    return frozenset()


_RAW_READER_ATTRIBUTES = {"open", "load", "loads", "read", "read_bytes", "read_file", "read_string", "read_text"}


def _python_reader_lines(scope: _PythonScope, inherited: dict[str, frozenset[str]], inherited_parsers: set[str],
                         paths: _PythonConfigPaths, *, shared_text_reader: bool = False
                         ) -> Iterator[tuple[int, frozenset[str]]]:
    names = _config_names(scope, inherited, paths)
    parsers = inherited_parsers | {target.id for target, value in scope.bindings
                                   if _config_parser(value, paths.imports)}
    for node in scope.reads:
        if isinstance(node, ast.Call) and (files := _raw_python_read(node, names, parsers, paths)):
            yield node.lineno, files
    for node, child in scope.children:
        if isinstance(node, ast.ClassDef):
            yield from _python_reader_lines(child, names, parsers, paths)
        else:
            path_parameters = {argument.arg: files for argument, value in _parameter_defaults(node.args)
                               if (files := paths.files(value, names))}
            if shared_text_reader and isinstance(node, ast.FunctionDef) and node.name == "read_text":
                path_parameters.update((argument.arg, _UNKNOWN_CONFIG_FILE) for argument in node.args.args[:1])
            yield from _python_reader_lines(child, names | path_parameters, parsers, paths)


def _shell_line_text(raw: str, quote: str) -> tuple[str, str, str, bool]:
    expanded: list[str] = []
    start = 0
    cursor = 0
    stop = len(raw)
    continued = False
    while match := _SHELL_SPECIAL.search(raw, cursor):
        position = match.start()
        token = match[0]
        cursor = position + 1
        if quote == "'":
            if token == quote:
                start = cursor
                quote = ""
        elif token == "\\":
            if cursor == stop:
                stop = position
                continued = True
                break
            if not quote or raw[cursor] in '`"$\\':
                expanded.append(raw[start:position])
                expanded.append(" ")
                cursor += 1
                start = cursor
        elif token == quote:
            quote = ""
        elif not quote and token in "\"'":
            quote = token
            if quote == "'":
                expanded.append(raw[start:position])
                expanded.append(" ")
        elif not quote and token == "#" and (position == 0 or raw[position - 1].isspace()):
            stop = position
            break
    if quote != "'":
        expanded.append(raw[start:stop])
    return raw[:stop], "".join(expanded), quote, continued


class _ShellLine(NamedTuple):
    number: int
    code: str
    expansions: str
    continuations: tuple[int, ...] = ()
    opening_quote: str = ""


def _shell_lines(source: _SourceFile) -> Iterator[_ShellLine]:
    heredoc_end = ""
    heredoc_expands = False
    quote = ""
    opening_quote = ""
    first = 1
    code_parts: list[str] = []
    expansions: list[str] = []
    for number, raw in enumerate(source.lines, start=1):
        if heredoc_end:
            if raw.lstrip("\t") == heredoc_end:
                heredoc_end = ""
            elif heredoc_expands:
                yield _ShellLine(number, "", raw)
            continue
        if not code_parts:
            first = number
            opening_quote = quote
        code, expanded, quote, continued = _shell_line_text(raw, quote)
        code_parts.append(code)
        expansions.append(expanded)
        if continued:
            continue
        logical = "".join(code_parts)
        if not quote and (marker := _SHELL_HEREDOC.search(logical)):
            heredoc_end = marker["end"]
            heredoc_expands = not marker["quote"]
        offsets: list[int] = []
        offset = 0
        for part in code_parts[:-1]:
            offset += len(part)
            offsets.append(offset)
        yield _ShellLine(first, logical, "".join(expansions), tuple(offsets), opening_quote)
        code_parts.clear()
        expansions.clear()


def _shell_config_files(text: str, names: dict[str, frozenset[str]], known_filenames: set[str]) -> frozenset[str]:
    files: set[str] = set()
    for match in _SHELL_OPERAND.finditer(text):
        operand = match[0].strip("\"'")
        if match[0].startswith("'"):
            expanded = operand
            inherited: set[str] = set()
        else:
            inherited = {name for variable in _SHELL_VARIABLE.finditer(operand)
                         for name in names.get(variable[1], frozenset())}
            expanded = re.sub(r"(?<!\\)\$(?:\{[^}]*\}|[a-zA-Z_][a-zA-Z0-9_]*)", "*", operand)
        direct = _config_path_files(operand, known_filenames) or _config_path_files(expanded, known_filenames)
        if not direct and (filename := expanded.rsplit("/", 1)[-1]).endswith(".conf"):
            direct = frozenset({filename}) if _CONFIG_FILENAME.fullmatch(filename) else _UNKNOWN_CONFIG_FILE
        if concrete := direct - _UNKNOWN_CONFIG_FILE:
            files.update(concrete)
        else:
            files.update(inherited or direct)
    return frozenset(files)


def _shell_reader_lines(lines: Sequence[_ShellLine], known_filenames: set[str]) -> Iterator[tuple[int, frozenset[str]]]:
    names: dict[str, frozenset[str]] = {}
    for number, line, _, continuations, opening_quote in lines:
        if "--rawfile" in line or "--slurpfile" in line:
            # Start in the quote that the shell scan carried from the previous line.
            for read in _SHELL_JQ_FILE_READ.finditer(opening_quote + line):
                position = read.start() - len(opening_quote)
                if position < 0 or not (path := read["path"]):
                    continue
                if files := _shell_config_files(path, names, known_filenames):
                    yield number + sum(offset <= position for offset in continuations), files
        if _SHELL_READ.search(line) and (files := _shell_config_files(line, names, known_filenames)):
            yield number, files
        if assignment := _SHELL_ASSIGNMENT.search(line):
            value = line[assignment.end():]
            if not value.lstrip("\"'").startswith("$(") and (files := _shell_config_files(value, names, known_filenames)):
                name = assignment[1]
                names[name] = names.get(name, frozenset()) | files


class _ReaderRead(NamedTuple):
    violation: Violation
    files: frozenset[str]


def _reader_violations(source: _SourceFile,
                       lines: Iterator[tuple[int, frozenset[str]]]) -> Iterator[_ReaderRead]:
    yield from (_ReaderRead(source.violation(number, "reader"), files)
                for number, files in sorted(set(lines), key=lambda read: (read[0], sorted(read[1]))))


def _config_filenames(root: Path) -> set[str]:
    return {path.name for path in (root / "config").rglob("*") if path.is_file()}


def check_config_readers(root: Path, files: Sequence[Path]) -> list[_ReaderRead]:
    return _scan_code(root, files).readers


def _reader_problems(readers: Sequence[_ReaderRead], rows: Sequence[exceptions.ReaderException]) -> Iterator[str]:
    allowed: dict[Path, list[frozenset[str]]] = {}
    for row in rows:
        allowed.setdefault(Path(row.path), []).append(frozenset(row.reads))
    found: dict[Path, set[str]] = {}
    for violation, names in readers:
        found.setdefault(violation.path, set()).update(names)
        choices = allowed.get(violation.path, ())
        if not any(names <= choice for choice in choices):
            missing = names.difference(*choices) or names
            yield f"{violation.diagnostic()} [{', '.join(sorted(missing))}]"
    for path, choices in allowed.items():
        if path not in found:
            yield f"{path}: reader: stale exception"
        else:
            for names in choices:
                if stale := names - found[path]:
                    yield f"{path}: reader: stale exception for {', '.join(sorted(stale))}"


def _is_config_variable(name: str) -> bool:
    return name.endswith(("_CONFIG", "_CONFIG_FILE"))


def _variable_name(node: ast.AST, assigned_names: dict[str, str]) -> str:
    return assigned_names.get(node.id, "") if isinstance(node, ast.Name) else _string(node)


def _is_environ(node: ast.AST, imports: dict[str, str], aliases: set[str]) -> bool:
    return (isinstance(node, ast.Name) and node.id in aliases) or _dotted_name(node, imports) == "os.environ"


def _python_variable_names(node: ast.Call | ast.Compare | ast.Subscript, imports: dict[str, str],
                           assigned_names: dict[str, str], aliases: set[str]) -> Iterator[tuple[int, str]]:
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
        if _is_environ(node.value, imports, aliases):
            yield node.lineno, _variable_name(node.slice, assigned_names)
    elif isinstance(node, ast.Call):
        function = node.func
        if _dotted_name(function, imports) == "os.getenv" or (
            isinstance(function, ast.Attribute) and function.attr in {"get", "setdefault", "pop"}
            and _is_environ(function.value, imports, aliases)
        ):
            if (key := _call_argument(node, "key")) is not None:
                yield node.lineno, _variable_name(key, assigned_names)
    elif isinstance(node, ast.Compare):
        for left, operator, right in zip((node.left, *node.comparators), node.ops, node.comparators):
            if isinstance(operator, (ast.In, ast.NotIn)) and _is_environ(right, imports, aliases):
                yield node.lineno, _variable_name(left, assigned_names)


def _python_variable_reads(scope: _PythonScope, imports: dict[str, str],
                           inherited_names: dict[str, str], inherited_aliases: set[str]) -> Iterator[tuple[int, str]]:
    assigned_names = inherited_names.copy()
    aliases = inherited_aliases.copy()
    for target, value in scope.bindings:
        if _is_config_variable(name := _string(value)):
            assigned_names[target.id] = name
        if _is_environ(value, imports, aliases):
            aliases.add(target.id)
    for node in scope.reads:
        yield from _python_variable_names(node, imports, assigned_names, aliases)
    for definition, child in scope.children:
        child_aliases = aliases
        if not isinstance(definition, ast.ClassDef):
            child_aliases = aliases | {argument.arg for argument, value in _parameter_defaults(definition.args)
                                       if _is_environ(value, imports, aliases)}
        yield from _python_variable_reads(child, imports, assigned_names, child_aliases)


class _VariableRead(NamedTuple):
    violation: Violation
    name: str


def _config_variable_reads(source: _SourceFile, reads: Iterator[tuple[int, str]]) -> Iterator[_VariableRead]:
    for number, name in sorted(set(reads)):
        if _is_config_variable(name):
            yield _VariableRead(source.violation(number, "variable"), name)


def check_config_variables(root: Path, files: Sequence[Path]) -> list[Violation]:
    return list(dict.fromkeys(read.violation for read in _scan_code(root, files).variables))


_LITERAL_NAME = re.compile(r"(?:MAX_|[A-Z0-9_]*(?:_MAX|_ATTEMPTS|_LIMIT))[A-Z0-9_]*|[A-Z0-9_]+_(?:BYTES|CHARS|RETRIES)")
_POSSIBLE_LITERAL_TARGET = rf"({_LITERAL_NAME.pattern})\s*[,):=\]]"
_LITERAL_HINT = re.compile(
    rf"(?m)(?:^|;)[ \t]*(?:[\[(][ \t]*|\*?[a-zA-Z_]\w*[ \t]*[,=][ \t]*)*?"
    + _POSSIBLE_LITERAL_TARGET
)
_ADDITIONAL_LITERAL_TARGET = re.compile(r"\b" + _POSSIBLE_LITERAL_TARGET)
_WAIT_PARTS = {"TIMEOUT", "WAIT", "GRACE", "INTERVAL"}
_TIME_UNITS = {"SECONDS", "S", "MINUTES", "MS", "HOURS", "MILLISECONDS"}


def _is_wait_name(name: str) -> bool:
    parts = name.split("_")
    return parts[-1] in _TIME_UNITS or bool(_WAIT_PARTS.intersection(parts[1:]))


def _has_literal_hint(text: str) -> bool:
    for match in _LITERAL_HINT.finditer(text):
        if not _is_wait_name(match[1]):
            return True
        if match[0][-1] in ",=":
            newline = text.find("\n", match.end())
            end = len(text) if newline == -1 else newline
            if any(not _is_wait_name(target[1]) for target in _ADDITIONAL_LITERAL_TARGET.finditer(text, match.end(), end)):
                return True
    return False


def _numeric_expression(expression: ast.expr) -> bool:
    if isinstance(expression, ast.Constant):
        return type(expression.value) in {int, float, complex}
    if isinstance(expression, ast.UnaryOp):
        return isinstance(expression.op, (ast.UAdd, ast.USub, ast.Invert)) and _numeric_expression(expression.operand)
    if isinstance(expression, ast.BinOp):
        return (not isinstance(expression.op, ast.MatMult)
                and _numeric_expression(expression.left) and _numeric_expression(expression.right))
    return False


def _time_constants(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        parameters = [argument.value for argument in node.keywords if argument.arg in {"timeout", "time_limit"}]
        function = node.func
        name = function.attr if isinstance(function, ast.Attribute) else function.id if isinstance(function, ast.Name) else ""
        if name in {"sleep", "timedelta", "CommandTimeLimit"}:
            parameters.extend(node.args)
            parameters.extend(argument.value for argument in node.keywords)
        names.update(parameter.id for parameter in parameters if isinstance(parameter, ast.Name))
    return names


class _LiteralDefinition(NamedTuple):
    violation: Violation
    name: str
    value: str


def _expression_text(source: _SourceFile, expression: ast.expr) -> str:
    start = expression.lineno - 1
    end = expression.end_lineno
    if end is None or expression.end_col_offset is None:
        return ast.unparse(expression)
    lines = [line.encode() for line in source.lines[start:end]]
    lines[-1] = lines[-1][:expression.end_col_offset]
    lines[0] = lines[0][expression.col_offset:]
    return b"\n".join(lines).decode()


def _literal_definitions(source: _SourceFile, tree: ast.Module,
                         bindings: Iterator[tuple[ast.Name, ast.expr]]) -> Iterator[_LiteralDefinition]:
    candidates = [(target, value) for target, value in bindings
                  if _LITERAL_NAME.fullmatch(target.id) and not _is_wait_name(target.id)
                  and _numeric_expression(value)]
    if candidates:
        times = _time_constants(tree)
        for target, value in candidates:
            if target.id not in times:
                yield _LiteralDefinition(source.violation(target.lineno, "literal"), target.id,
                                         _expression_text(source, value))


def check_tunable_literals(root: Path, files: Sequence[Path]) -> list[_LiteralDefinition]:
    return _scan_code(root, files).literals


class _CodeScan(NamedTuple):
    readers: list[_ReaderRead]
    variables: list[_VariableRead]
    literals: list[_LiteralDefinition]


def _scan_python(source: _SourceFile, scan: _CodeScan, known_filenames: set[str],
                 *, readers: bool, variables: bool) -> None:
    literals = _has_literal_hint(source.text)
    if not (readers or variables or literals):
        return
    tree = ast.parse(source.text, filename=str(source.path))
    if readers or variables:
        imports: dict[str, str] = {}
        scope = _python_scope(tree.body, imports)
        if readers:
            reads = _python_reader_lines(scope, {}, set(), _PythonConfigPaths(imports, known_filenames),
                                        shared_text_reader=source.path == _SHARED_TEXT_READER)
            scan.readers.extend(_reader_violations(source, reads))
        if variables:
            scan.variables.extend(_config_variable_reads(source, _python_variable_reads(scope, imports, {}, set())))
        bindings = iter(scope.bindings)
    else:
        bindings = (pair for node in _scope_nodes(tree.body) for pair in _assignments(node))
    if literals:
        scan.literals.extend(_literal_definitions(source, tree, bindings))


def _scan_code(root: Path, files: Sequence[Path]) -> _CodeScan:
    known_filenames = _config_filenames(root)
    filenames = "|".join(map(re.escape, sorted(known_filenames)))
    filename_hint = rf"""['"/](?:{filenames})(?![\w.-])""" if filenames else ""
    config_hint = re.compile("|".join(filter(None, (_CONFIG_PATH_HINT, _PYTHON_CONFIG_HINT, filename_hint))))
    scan = _CodeScan([], [], [])
    for source in _source_files(root, files, _SHELL_SUFFIXES | {".py"}):
        if _python_source(source):
            readers = (source.path not in _MODEL_READERS and bool(_RAW_PYTHON_HINT.search(source.text))
                       and bool(config_hint.search(source.text)))
            _scan_python(
                source, scan, known_filenames,
                readers=readers,
                variables=bool(_CONFIG_VARIABLE_HINT.search(source.text) and _ENVIRONMENT_HINT.search(source.text)),
            )
        else:
            readers = source.path not in _MODEL_READERS and bool(config_hint.search(source.text))
            variables = "_CONFIG" in source.text
            if readers or variables:
                lines = list(_shell_lines(source))
                if readers:
                    scan.readers.extend(_reader_violations(source, _shell_reader_lines(lines, known_filenames)))
                if variables:
                    reads = ((line.number, match[1]) for line in lines
                             for match in _SHELL_VARIABLE.finditer(line.expansions))
                    scan.variables.extend(_config_variable_reads(source, reads))
    return scan


def _code_problems(root: Path, files: Sequence[Path]) -> Iterator[str]:
    readers, variables, literals = _scan_code(root, files)
    yield from _reader_problems(readers, exceptions.READER_EXCEPTIONS)

    variable_names = {entry.name for entry in exceptions.VARIABLE_EXCEPTIONS}
    for violation, name in variables:
        if name not in variable_names:
            yield f"{violation.diagnostic()} [{name}]"
    for name in sorted(variable_names - {read.name for read in variables}):
        yield f"{name}: variable: stale exception"

    literal_keys = {(Path(entry.path), entry.name) for entry in exceptions.LITERAL_INVENTORY}
    found_literals = {(definition.violation.path, definition.name) for definition in literals}
    for violation, name, value in literals:
        if (violation.path, name) not in literal_keys:
            yield f"{violation.diagnostic()} [{name} = {value}]"
    for path, name in sorted(literal_keys - found_literals):
        yield f"{path}: literal: stale inventory row for {name}"
    if len(literal_keys) != len(exceptions.LITERAL_INVENTORY):
        yield "literal: several inventory rows classify the same constant"


class ConfigFormTests(unittest.TestCase):
    def test_repository_form(self) -> None:
        root = Path(__file__).resolve().parents[2]
        tracked = subprocess.run(
            ["git", "ls-files", "-z", "config/", "scripts/"], cwd=root, check=True, capture_output=True,
            text=True, timeout=TEST_HANG_GUARD_SECS,
        )
        tracked_files = tracked.stdout.split("\0")
        files = [Path(path) for path in tracked_files if path.startswith("config/")]
        code_files = [Path(path) for path in tracked_files if path.startswith("scripts/")]

        # Collect violations before applying exceptions. This also finds stale exceptions.
        format_violations = check_format(root, files)
        suffix_violations = [violation for violation in format_violations if violation.line_number == 0]
        row_violations = [violation for violation in format_violations if violation.line_number != 0]
        key_violations = check_key_form(root, files)
        all_violations = (
            ("format", suffix_violations, exceptions.DATA_FILES + exceptions.PENDING_TOML_FILES),
            ("format", row_violations, exceptions.FORMAT_FILES),
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
                else:
                    problems.append(f"{path}: {rule}: stale file exception")

        for suffix in exceptions.DATA_SUFFIXES:
            matches = [violation for violation in suffix_violations if violation.path.suffix == suffix]
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
        problems.extend(_code_problems(root, code_files))
        if problems:
            self.fail("\n".join(problems))

    def test_planted_violations(self) -> None:
        large_number_row = f"amount={'9' * 310}"
        cases = (
            (check_format, "bad.ini", "value=text\n", 0, "format", "bad.ini"),
            (check_format, "example.conf", "marker: done\n", 1, "format", "marker: done"),
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
            (check_config_readers, "example.py", 'from pathlib import Path\nPath("config/example.conf").read_text()\n',
             2, "reader", 'Path("config/example.conf").read_text()'),
            (check_config_readers, "example.py",
             'from configparser import ConfigParser\nfrom scripts.lib import conf\n'
             + 'conf.read("example")\nConfigParser().read("config/example.conf")\n',
             4, "reader", 'ConfigParser().read("config/example.conf")'),
            (check_config_readers, "example.py",
             'import configparser\nparser = configparser.ConfigParser()\ndef read():\n'
             + '    parser.read("config/example.conf")\n',
             4, "reader", '    parser.read("config/example.conf")'),
            (check_config_readers, "example.py", 'open(file="config/gate.conf").read()\n',
             1, "reader", 'open(file="config/gate.conf").read()'),
            (check_config_readers, "example.py",
             'def read(text=Path("config/gate.conf").read_text()):\n    return text\n',
             1, "reader", 'def read(text=Path("config/gate.conf").read_text()):'),
            (check_config_readers, "example.sh", 'cat ' + "\\" + '\n  "config/gate.conf"\n',
             1, "reader", 'cat ' + "\\"),
            (check_config_readers, "example.sh", 'path="config/example.conf"\nconf_value example mode\ncat "$path"\n',
             3, "reader", 'cat "$path"'),
            (check_config_variables, "example.sh",
             "printf '%s\\n' '$MENTION_CONFIG_FILE'\n" + 'printf "%s\\n" "${EXAMPLE_CONFIG_FILE:-}"\n',
             2, "variable", 'printf "%s\\n" "${EXAMPLE_CONFIG_FILE:-}"'),
            (check_config_variables, "example.py",
             'from os import getenv as read_env\nread_env("CONF_DIR")\nread_env("EXAMPLE_CONFIG")\n',
             3, "variable", 'read_env("EXAMPLE_CONFIG")'),
            (check_config_variables, "example.py",
             'import os\nenvironment = os.environ\nenvironment.get("EXAMPLE_CONFIG_FILE")\n',
             3, "variable", 'environment.get("EXAMPLE_CONFIG_FILE")'),
            (check_config_variables, "example.py",
             'import os\ndef read(environment: Mapping[str, str] = os.environ):\n'
             + '    environment.get("EXAMPLE_CONFIG_FILE")\n',
             3, "variable", '    environment.get("EXAMPLE_CONFIG_FILE")'),
            (check_config_variables, "example.sh",
             "printf '%s' 'text\n$REPORT_CONFIG'\n" + 'printf "%s" "text\n$REPORT_CONFIG"\n',
             4, "variable", '$REPORT_CONFIG"'),
            (check_tunable_literals, "example.py",
             "MAX_WAIT_SECONDS = 30\nBUFFER_MAX_BYTES = 64 * 1024\ndef write():\n    MAX_CHARS = 20\n",
             2, "literal",
             "BUFFER_MAX_BYTES = 64 * 1024"),
            (check_tunable_literals, "example.py",
             "RELEASE_LOCK_WAIT_S = 60\nFLAG_WAIT = 60.0\nMAX_WAITING_JOBS = 20\n",
             3, "literal", "MAX_WAITING_JOBS = 20"),
            (check_tunable_literals, "example.py", "WAIT_WORK_SHARE_LIMIT = 0.10\n",
             1, "literal", "WAIT_WORK_SHARE_LIMIT = 0.10"),
        )
        for check, name, source, number, rule, line in cases:
            with self.subTest(rule=rule, line=line), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                path = Path("scripts" if Path(name).suffix in {".py", ".sh"} else "config") / name
                (root / path).parent.mkdir()
                _ = (root / path).write_text(source, encoding="utf-8")
                files = [path]
                if path.suffix == ".conf" and check is not check_format:
                    data = path.with_suffix(".md")
                    _ = (root / data).write_text(source, encoding="utf-8")
                    files.append(data)
                actual = [item.violation if isinstance(item, (_ReaderRead, _LiteralDefinition)) else item
                          for item in check(root, files)]
                self.assertEqual(actual, [Violation(path, number, rule, line)])

        for source, expected in (
            ("MAX_ROWS, MAX_COLS = 100, 80", [("MAX_ROWS", "100"), ("MAX_COLS", "80")]),
            ("[MAX_ROWS, *MAX_COLS] = [100, 80]", [("MAX_ROWS", "100")]),
            ("LOCK_WAIT_S, MAX_COLS = 60, 80", [("MAX_COLS", "80")]),
        ):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                path = Path("example.py")
                _ = (root / path).write_text(source + "\n", encoding="utf-8")
                self.assertEqual(check_tunable_literals(root, [path]), [
                    _LiteralDefinition(Violation(path, 1, "literal", source), name, value)
                    for name, value in expected
                ])

        with self.subTest(rule="reader exception"), tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = Path("scripts/lib/names.py")
            (root / path).parent.mkdir(parents=True)
            _ = (root / path).write_text(
                'open("config/names.toml")\npath = Path("config/notify.conf")\npath.read_text()\n',
                encoding="utf-8",
            )
            row = exceptions.ReaderException(str(path), ("names.toml",), "Read names data.",
                                             exceptions.ExceptionLifetime.PERMANENT)
            problems = list(_reader_problems(check_config_readers(root, [path]), [row]))
            self.assertEqual(problems, [f"{path}:3: reader: path.read_text() [notify.conf]"])

        continued_jq = 'settings="config/statusline.conf"\njq \\\n'
        for prefix, command, number in (
            (continued_jq, '  --rawfile settings "$settings" .', 3),
            (continued_jq, '  --slurpfile settings "$settings" .', 3),
            ("", "printf '%s\\n' 'jq --rawfile colors config/statusline.conf .'", 0),
            ("", 'jq "--rawfile" settings config/statusline.conf \'.\'', 1),
        ):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                path = Path("example.sh")
                _ = (root / path).write_text(f"{prefix}{command}\n", encoding="utf-8")
                self.assertEqual(check_config_readers(root, [path]), [
                    _ReaderRead(Violation(path, number, "reader", command), frozenset({"statusline.conf"})),
                ] if number else [])

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
