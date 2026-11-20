"""Share shell setup for isolated caller tests."""

from pathlib import Path
import re


def fixture_zsh_environment() -> dict[str, str]:
    """Keep the fixture PATH through NixOS zsh startup."""
    return {"__NIXOS_SET_ENVIRONMENT_DONE": "1", "__ETC_ZSHENV_SOURCED": "1"}


def shell_function(source: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^{re.escape(name)}\(\) (?:(?P<brace>\{{)|\()\n.*?^(?(brace)\}}|\))",
        source,
    )
    if match is None:
        raise AssertionError(f"missing shell function: {name}")
    return match.group(0)


def shell_functions(path: Path, *names: str) -> str:
    """Return shell functions in the requested dependency order."""
    if not names:
        raise ValueError("At least one shell function name is required")
    source = path.read_text(encoding="utf-8")
    return "".join(shell_function(source, name) + "\n" for name in names)
