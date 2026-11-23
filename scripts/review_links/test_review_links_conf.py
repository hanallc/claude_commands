"""Test that each review_links.conf key the review_links modules read has a row in the repository file."""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
import unittest

from scripts.lib import conf


PACKAGE = Path(__file__).resolve().parent
PACKAGE_NAME = "scripts.review_links"
REPOSITORY_FILE = PACKAGE.parents[1] / "config" / "review_links.conf"
READERS = frozenset({"read", "read_or_repository"})
# These ConfigRead methods take one key as the first argument.
KEY_METHODS = frozenset({"text", "number", "whole", "value"})


@dataclass
class Module:
    """One module and the names it gives to other modules of the package."""

    name: str
    aliases: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class KeyRead:
    """One key method call, such as settings.whole(key), on a possible settings object."""

    place: str
    receiver: ast.expr
    key: ast.expr


@dataclass(frozen=True)
class PackageCall:
    """One call to a package function, with each positional argument paired to its parameter."""

    place: str
    callee: Function
    arguments: tuple[tuple[str, ast.expr], ...]


@dataclass(eq=False)
class Function:
    """One module function or class method, with the parameters that hold review_links settings or keys."""

    module: Module
    parameters: tuple[str, ...]
    settings_names: set[str] = field(default_factory=set)
    key_parameters: set[str] = field(default_factory=set)
    key_reads: list[KeyRead] = field(default_factory=list)
    calls: list[ast.Call] = field(default_factory=list)
    package_calls: list[PackageCall] = field(default_factory=list)


def literal(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def reads_file(node: ast.expr) -> bool:
    """Match conf.read("review_links") and conf.read_or_repository("review_links")."""
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in READERS
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "conf"
            and len(node.args) == 1 and literal(node.args[0]) == "review_links")


def holds_settings(node: ast.expr, settings: set[str]) -> bool:
    return reads_file(node) or (isinstance(node, ast.Name) and node.id in settings)


def scan(module: Module, node: ast.FunctionDef, *, method: bool) -> Function:
    """Walk one function once. Keep its settings names, its key reads and its calls."""
    names = tuple(argument.arg for argument in (*node.args.posonlyargs, *node.args.args))
    function = Function(module, names[1:] if method else names)
    for child in ast.walk(node):
        if isinstance(child, ast.Assign) and reads_file(child.value):
            function.settings_names.update(name.id for name in child.targets if isinstance(name, ast.Name))
        elif isinstance(child, ast.Call):
            function.calls.append(child)
            if isinstance(child.func, ast.Attribute) and child.func.attr in KEY_METHODS and child.args:
                function.key_reads.append(KeyRead(f"{module.name}.py:{child.lineno}", child.func.value, child.args[0]))
    return function


def functions() -> dict[tuple[str, str], Function]:
    """Parse each package module once and index its module functions and class methods."""
    index: dict[tuple[str, str], Function] = {}
    for source in sorted(PACKAGE.glob("*.py")):
        if source.name.startswith("test_"):
            continue
        module = Module(source.stem)
        for node in ast.parse(source.read_text(encoding="utf-8"), filename=str(source)).body:
            if isinstance(node, ast.ImportFrom) and node.module == PACKAGE_NAME:
                module.aliases.update({name.asname or name.name: name.name for name in node.names})
            elif isinstance(node, ast.FunctionDef):
                index[module.name, node.name] = scan(module, node, method=False)
            elif isinstance(node, ast.ClassDef):
                for member in node.body:
                    if isinstance(member, ast.FunctionDef):
                        index[module.name, f"{node.name}.{member.name}"] = scan(module, member, method=True)
    return index


def target(module: Module, call: ast.Call, index: dict[tuple[str, str], Function]) -> Function | None:
    """Find the package function a call names: f, module.f or module.Class.f."""
    match call.func:
        case ast.Name(id=name):
            return index.get((module.name, name))
        case ast.Attribute(value=ast.Name(id=owner), attr=name) if owner in module.aliases:
            return index.get((module.aliases[owner], name))
        case ast.Attribute(value=ast.Attribute(value=ast.Name(id=owner), attr=cls), attr=name) if owner in module.aliases:
            return index.get((module.aliases[owner], f"{cls}.{name}"))
        case _:
            return None


def read_keys() -> dict[str, set[str]]:
    """Return each literal key read from review_links.conf, with the places that read it.

    A settings object is a conf.read or conf.read_or_repository call on "review_links", a local name bound to one,
    or a function parameter that a package call fills with one. A key is the literal first argument of a key method
    on a settings object, or a literal that a call passes to a parameter that the function uses as a key on such an
    object.
    """
    index = functions()
    for function in index.values():
        for call in function.calls:
            if (callee := target(function.module, call, index)) is not None:
                place = f"{function.module.name}.py:{call.lineno}"
                passed = tuple(zip(callee.parameters, call.args, strict=False))
                function.package_calls.append(PackageCall(place, callee, passed))
    found: dict[str, set[str]] = {}
    changed = True
    while changed:
        changed = False
        for function in index.values():
            for read in function.key_reads:
                if not holds_settings(read.receiver, function.settings_names):
                    continue
                if (key := literal(read.key)) is not None:
                    found.setdefault(key, set()).add(read.place)
                elif (isinstance(read.key, ast.Name) and read.key.id in function.parameters
                      and read.key.id not in function.key_parameters):
                    function.key_parameters.add(read.key.id)
                    changed = True
            for call in function.package_calls:
                if not any(holds_settings(argument, function.settings_names) for _, argument in call.arguments):
                    continue
                for name, argument in call.arguments:
                    if holds_settings(argument, function.settings_names):
                        if name not in call.callee.settings_names:
                            call.callee.settings_names.add(name)
                            changed = True
                    elif name in call.callee.key_parameters and (key := literal(argument)) is not None:
                        found.setdefault(key, set()).add(call.place)
    return found


class ReviewLinksConfTest(unittest.TestCase):
    def test_each_read_key_has_a_row_in_the_repository_file(self) -> None:
        rows = conf.shipped_keys(REPOSITORY_FILE)
        found = read_keys()
        # The scan must find the keys of the known read forms, or an empty result passes in silence.
        # Each scan branch loses at least one of these keys when it stops working.
        for key in ("server_port", "accounts_page_seconds", "tailscale_timeout_seconds", "card_fade_in_ms",
                    "later_morning", "later_week_time", "page_delivery_window_seconds", "operator_login"):
            self.assertIn(key, found)
        missing = {key: sorted(places) for key, places in found.items() if key not in rows}
        self.assertEqual(missing, {}, f"{REPOSITORY_FILE} has no row for these keys")


if __name__ == "__main__":
    _ = unittest.main()
