"""Identify test starts from command arguments."""

from collections.abc import Sequence
from os.path import basename


SHELLS = frozenset({"bash", "sh", "dash", "zsh"})
PYTEST_COMMANDS = frozenset({"pytest", "py.test"})
NODE_VALUE_OPTIONS = frozenset({
    "-r", "--require", "--import", "--loader", "--experimental-loader",
    "-C", "--conditions", "--env-file", "--env-file-if-exists", "--icu-data-dir",
})


def is_python_command(program: str) -> bool:
    if program == "scripts/lib/py" or program.endswith("/scripts/lib/py"):
        return True
    name = basename(program)
    if name == "python":
        return True
    return name.startswith("python") and all(
        part.isdecimal() for part in name.removeprefix("python").split(".")
    )


def python_target(arguments: tuple[str, ...]) -> tuple[str, ...]:
    """Read the Python entry point and its arguments."""
    options = iter(enumerate(arguments))
    for index, argument in options:
        if argument == "--":
            return arguments[index + 1:]
        if argument == "-" or not argument.startswith("-"):
            return arguments[index:]
        if argument == "--check-hash-based-pycs":
            _ = next(options, None)
        elif not argument.startswith("--"):
            for offset, flag in enumerate(argument[1:], 2):
                value = argument[offset:]
                if flag in "mc":
                    return (f"-{flag}", value, *arguments[index + 1:]) if value else arguments[index:]
                if flag in "WX":
                    if not value:
                        _ = next(options, None)
                    break
    return ()


def shell_target(arguments: tuple[str, ...]) -> tuple[bool, tuple[str, ...]]:
    """Read a shell command string or the script path."""
    options = iter(enumerate(arguments))
    for index, argument in options:
        if argument == "--":
            return False, arguments[index + 1:]
        if argument in {"-o", "+o", "-O", "+O"}:
            _ = next(options, None)
        elif argument == "-" or not argument.startswith("-"):
            return False, arguments[index:]
        elif not argument.startswith("--") and "c" in argument:
            return True, arguments[index + 1:]
    return False, ()


def node_script(arguments: tuple[str, ...]) -> str:
    """Read the Node script after the runtime options."""
    options = iter(enumerate(arguments))
    for index, argument in options:
        if argument == "--":
            return arguments[index + 1] if index + 1 < len(arguments) else ""
        if not argument.startswith("-"):
            return argument
        if (argument in {"-", "--eval", "--print"}
                or argument.startswith(("-e", "-p", "--eval=", "--print="))):
            return ""
        if argument in NODE_VALUE_OPTIONS:
            _ = next(options, None)
    return ""


def is_test_start(argv: Sequence[str]) -> bool:
    """Check whether the command starts a test or the module runner."""
    if not argv:
        return False
    program = argv[0]
    name = basename(program)
    if name in SHELLS:
        inline, target = shell_target(tuple(argv[1:]))
        return not inline and is_test_start(target)
    if name in {"node", "nodejs"}:
        name = basename(node_script(tuple(argv[1:])))
        return "proof" in name and name.endswith(".mjs")
    if is_python_command(program):
        target = python_target(tuple(argv[1:]))
        if not target or target[0] == "-c":
            return False
        if target[0] == "-m":
            if len(target) < 2:
                return False
            module = target[1]
            return (module.partition(".")[0] in {"unittest", "pytest"}
                    or module == "scripts.test_times.module_run"
                    or module.rsplit(".", 1)[-1].startswith("test_"))
        name = basename(target[0])
    return (name in PYTEST_COMMANDS or name == "module_run.py"
            or name.startswith("test_") and name.endswith((".py", ".sh"))
            or "proof" in name and name.endswith(".mjs"))
