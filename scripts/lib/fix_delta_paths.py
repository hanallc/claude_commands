"""Validate the relative destinations in a fix delta."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast
from unicodedata import category, normalize


_METADATA = frozenset(('.git', '.gitmodules', '.gitignore', '.gitattributes',
                       '.hg', '.hgignore', '.hgtags', '.svn', '.bzr',
                       '.claude', '.codex', '.direnv'))
_CARGO_CONFIG = frozenset(('config', 'config.toml'))


@dataclass(frozen=True)
class FixDeltaPath:
    """Keep the validated path and its original components."""

    path: str
    parts: tuple[str, ...]


@dataclass(frozen=True)
class _FileDestination:
    """Reserve a file name in its parent directory."""


@dataclass(frozen=True)
class _DeltaDirectory:
    spelling: str
    children: dict[str, _DeltaDirectory | _FileDestination]


def validate_fix_delta_paths(paths: Sequence[str]) -> tuple[FixDeltaPath, ...]:
    """Validate each path and the destinations they share."""
    if isinstance(paths, str):
        raise ValueError('Fix delta paths must be a sequence of paths')
    root: dict[str, _DeltaDirectory | _FileDestination] = {}
    validated: list[FixDeltaPath] = []
    for path in cast(Sequence[object], paths):
        if not isinstance(path, str):
            raise ValueError('A fix delta path must be text')
        _ = path.encode('utf-8')
        if path != normalize('NFC', path) or any(
                char in '\\:' or category(char) == 'Cc' for char in path):
            raise ValueError(f'Invalid fix delta path: {path!r}')
        parts = tuple(path.split('/'))
        if any(not part or part in ('.', '..') or part.endswith(('.', ' ')) for part in parts):
            raise ValueError(f'Invalid fix delta path: {path!r}')
        key = tuple(normalize('NFC', part.casefold()) for part in parts)
        if any(part in _METADATA for part in key) or any(
                parent == '.cargo' and name in _CARGO_CONFIG for parent, name in zip(key, key[1:])):
            raise ValueError(f'Fix delta path enters repository metadata: {path!r}')
        parent_entries = root
        for part, folded in zip(parts[:-1], key[:-1], strict=True):
            directory = parent_entries.get(folded)
            if directory is None:
                directory = _DeltaDirectory(part, {})
                parent_entries[folded] = directory
            if isinstance(directory, _FileDestination) or directory.spelling != part:
                raise ValueError(f'Conflicting fix delta directory: {path!r}')
            parent_entries = directory.children
        if key[-1] in parent_entries:
            raise ValueError(f'Conflicting fix delta destination: {path!r}')
        parent_entries[key[-1]] = _FileDestination()
        validated.append(FixDeltaPath(path, parts))
    return tuple(validated)
