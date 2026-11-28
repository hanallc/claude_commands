"""Prepare the current files for a cloud gate."""
from __future__ import annotations

from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import file_digest
import json
from pathlib import Path
import re
import subprocess
import tempfile
from typing import cast

from scripts.aws_target import ledger
from scripts.aws_target.controller_lock import account_stack_lock
from scripts.lib.file_lock import LockResult


SOURCE_ARCHIVE = 'current-files.pack'
SOURCE_FORM = 'current-files'
NIX_BINARY_CACHE = 'https://cache.nixos.org'
STORE_PATH_PATTERN = r'/nix/store/[0-9a-z]{32}-[a-zA-Z0-9+._?=-]+'


class GateToolsUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class PublicCloneSource:
    pass


@dataclass(frozen=True)
class CurrentFilesSource:
    tool_paths: tuple[str, ...]


PUBLIC_CLONE_SOURCE = PublicCloneSource()


def tool_paths(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise GateToolsUnavailable('GateToolsUnavailable: the gate tool paths are absent')
    paths = cast(list[object], value)
    if any(not isinstance(path, str) or not re.fullmatch(STORE_PATH_PATTERN, path) for path in paths):
        raise GateToolsUnavailable('GateToolsUnavailable: a gate tool path is invalid')
    return tuple(dict.fromkeys(cast(list[str], paths)))


def deployment_source(deployment: dict[str, object]) -> CurrentFilesSource | PublicCloneSource:
    if 'source_tool_paths' not in deployment:
        return PUBLIC_CLONE_SOURCE
    return CurrentFilesSource(tool_paths(deployment['source_tool_paths']))


@contextmanager
def source_write_access(directory: Path, deployment_id: str) -> Generator[None]:
    """Check the live deployment under the lock before an AWS write."""
    with account_stack_lock(directory, shared=True) as access:
        if access is LockResult.BUSY:
            raise ValueError('The AWS account stack is busy. The gate cannot launch')
        try:
            manifest = ledger.mapping(cast(object, json.loads((directory / 'gate-deployment.json').read_text())))
        except FileNotFoundError:
            manifest = {}
        if ((directory / 'down.json').exists()
                or manifest.get('deployment_id') != deployment_id):
            raise ValueError('The AWS gate deployment is no longer available')
        yield


def prepare_tools(revision: str, packages: Sequence[str]) -> tuple[str, ...]:
    """Resolve the pinned Linux outputs and check the binary cache."""
    nix = ('nix', '--extra-experimental-features', 'nix-command flakes')
    if not re.fullmatch(r'[0-9a-f]{40}', revision) or not packages:
        raise GateToolsUnavailable('GateToolsUnavailable: no pinned packages')
    if any(not re.fullmatch(r'[a-zA-Z0-9_+-]+', package) for package in packages):
        raise GateToolsUnavailable('GateToolsUnavailable: an invalid package name')
    attrs = ' '.join(json.dumps(package) for package in packages)
    select = f'packages: builtins.map (name: (builtins.getAttr name packages).outPath) [ {attrs} ]'
    try:
        result = subprocess.check_output([*nix, 'eval', '--json', f'github:NixOS/nixpkgs/{revision}#legacyPackages.x86_64-linux',
                                          '--apply', select], timeout=300, text=True)
        paths = tool_paths(cast(object, json.loads(result)))
        _ = subprocess.check_output([*nix, 'path-info', '--recursive', '--store', NIX_BINARY_CACHE, *paths],
                                    timeout=300, text=True)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise GateToolsUnavailable('GateToolsUnavailable: pinned packages are unavailable in the Nix binary cache') from error
    return paths


def git(repo: Path, *arguments: str) -> str:
    return subprocess.check_output(['git', '--no-replace-objects', '-C', str(repo), *arguments], text=True).strip()


def archive(repo: Path, folder: Path, commits: Sequence[str]) -> None:
    """Pack the named commits and the objects in their trees."""
    tips = tuple(dict.fromkeys(commits))
    trees = tuple(f'{commit}^{{tree}}' for commit in tips)
    objects = git(repo, 'rev-list', '--objects', '--no-object-names', *trees)
    with tempfile.TemporaryDirectory(dir=folder) as scratch:
        path = Path(scratch) / SOURCE_ARCHIVE
        with path.open('wb') as output:
            _ = subprocess.run(['git', '--no-replace-objects', '-C', str(repo), 'pack-objects', '--stdout'],
                input='\n'.join((*tips, objects, '')), text=True, stdout=output, check=True)
        _ = path.replace(folder / SOURCE_ARCHIVE)


def request_commits(request: dict[str, object]) -> tuple[str, ...]:
    origin_main = ledger.text(request, 'origin_main')
    if request.get('task') == 'paired':
        return (ledger.text(ledger.mapping(request['base']), 'commit'),
                ledger.text(ledger.mapping(request['changed']), 'commit'), origin_main)
    return ledger.text(request, 'commit'), origin_main


def archive_path(folder: Path, request: dict[str, object]) -> Path:
    return (folder.parent if request.get('task') == 'paired' else folder) / SOURCE_ARCHIVE


def object_key(request: dict[str, object]) -> str:
    return 'source/' + ledger.text(request, 'attempt') + '.pack'


def archive_sha256(path: Path) -> str:
    with path.open('rb') as stream:
        return file_digest(stream, 'sha256').hexdigest()
