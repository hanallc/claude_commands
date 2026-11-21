#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Evaluate a flake's development shell once per input change, and read its programs.

The Linux SSH runner sources the whole shell. A test step on another host
(the Mac, or natedev outside direnv) adds only the shell's program folders to
the end of PATH, so every tool the host has stays first and only the programs
it lacks come from the shell.

Usage: dev_shell.py path --root <workspace root>
"""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from dataclasses import dataclass
import fcntl
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from scripts.lib import build_folders
else:
    from scripts.lib import build_folders

STORE_PATH = re.compile(r'/nix/store/[a-z0-9]{32}-[A-Za-z0-9+._?=-]+')
PATH_EXPORT = re.compile(r"^PATH='([^']*)'$", re.MULTILINE)


@dataclass(frozen=True)
class ShellEnvironment:
    """Exports published after a successful shell evaluation."""

    key: str
    exports: Path
    inputs: Path


def shell_inputs(root: Path) -> tuple[Path, ...]:
    """List the shell inputs without source files or build output."""
    paths = [root / 'flake.nix', root / 'flake.lock']
    nix = root / 'nix'
    if nix.is_symlink():
        raise ValueError('unsafe shell input directory: nix')
    if nix.exists():
        children = tuple(nix.rglob('*'))
        if any(path.is_symlink() for path in children):
            raise ValueError('shell inputs must not contain symbolic links')
        paths.extend(path for path in children if not path.is_dir())
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f'unsafe or missing shell input: {path}')
        for parent in path.relative_to(root).parents:
            if (root / parent).is_symlink():
                raise ValueError(f'unsafe shell input directory: {parent}')
    return tuple(sorted(paths, key=lambda path: path.relative_to(root).as_posix()))


def shell_key(root: Path, selector: str) -> str:
    digest = hashlib.sha256()
    digest.update(selector.encode() + b'\0')
    for path in shell_inputs(root):
        name = path.relative_to(root).as_posix().encode()
        content = path.read_bytes()
        digest.update(len(name).to_bytes(8, 'big') + name)
        digest.update(len(content).to_bytes(8, 'big') + content)
    return digest.hexdigest()


def exports_are_valid(exports: Path) -> bool:
    """Reevaluate exports when a referenced store path no longer exists."""
    text = exports.read_text()
    roots: list[str] = STORE_PATH.findall(text)
    return bool(text.strip()) and all(Path(root).exists() for root in roots)


def prepare_shell(root: Path, cache: Path, selector: str, timeout_s: float, *,
                  pass_fds: tuple[int, ...] = ()) -> ShellEnvironment:
    """Publish immutable shell inputs and exports under a content key."""
    key = shell_key(root, selector)
    cache.mkdir(parents=True, exist_ok=True)
    destination = cache / key
    with (cache / f'{key}.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        exports = destination / 'exports.sh'
        if exports.is_file() and exports_are_valid(exports):
            return ShellEnvironment(key, exports, destination / 'inputs')
        if not destination.exists():
            with build_folders.temporary_build_folder(build_folders.UniqueBuildFolder(
                    f".{key}.", build_folders.ExplicitBuildParent(cache))) as stage:
                inputs = stage / 'inputs'
                inputs.mkdir()
                for path in shell_inputs(root):
                    target = inputs / path.relative_to(root)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    _ = shutil.copyfile(path, target)
                if shell_key(inputs, selector) != key:
                    raise ValueError('shell inputs changed during the copy')
                build_folders.publish_build_folder(stage, destination)
        inputs = destination / 'inputs'
        if shell_key(inputs, selector) != key:
            raise ValueError('cached shell inputs do not match their key')
        result = subprocess.run(
            ['nix', 'print-dev-env', f'path:{inputs}#{selector}'],
            capture_output=True, text=True, check=True, timeout=timeout_s, pass_fds=pass_fds,
        )
        if not result.stdout.strip():
            raise ValueError('nix returned no shell exports')
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=destination, delete=False) as output:
            temporary = Path(output.name)
            try:
                _ = output.write(result.stdout)
                output.flush()
                os.replace(temporary, exports)
            finally:
                temporary.unlink(missing_ok=True)
    return ShellEnvironment(key, exports, destination / 'inputs')


def program_folders(shell: ShellEnvironment) -> str:
    """Join the shell's /nix/store program folders, in the shell's order."""
    exported: list[str] = PATH_EXPORT.findall(shell.exports.read_text())
    folders = exported[-1].split(':') if exported else []
    return ':'.join(folder for folder in folders if folder.startswith('/nix/store/'))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Print a flake development shell's program folders.")
    commands = parser.add_subparsers(dest='command', required=True)
    path = commands.add_parser('path')
    _ = path.add_argument('--root', type=Path, required=True)
    arguments = parser.parse_args(argv)
    from scripts.lib import conf

    settings = conf.read('dev_shell')
    root = cast(Path, arguments.root)
    try:
        shell = prepare_shell(root, Path.home() / settings.text('cache'), settings.text('selector'),
                              settings.number('timeout_s'))
    except subprocess.CalledProcessError as error:
        lines = (cast(str | None, error.stderr) or '').strip().splitlines()
        print(f"dev_shell: {root}: nix print-dev-env failed: {lines[-1] if lines else f'exit {error.returncode}'}",
              file=sys.stderr)
        return 1
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f'dev_shell: {root}: {error}', file=sys.stderr)
        return 1
    print(program_folders(shell))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
