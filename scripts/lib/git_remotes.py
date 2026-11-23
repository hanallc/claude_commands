"""Keep the upstream refs and the local branches of a source in two remotes of a copy."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol


class GitRun(Protocol):
    """Run one Git command in a repository, return its output, and raise when it fails."""

    def __call__(self, repo: Path, *arguments: str, data: bytes | None = None) -> bytes: ...


type GitSettings = dict[str, list[str]]


def read_git_settings(git: GitRun, repo: Path, *options: str) -> GitSettings:
    """Read each configuration key and its values in file order."""
    settings: GitSettings = {}
    for entry in os.fsdecode(git(repo, "config", *options, "--null", "--list")).split("\0"):
        if entry:
            key, _, value = entry.partition("\n")
            settings.setdefault(key, []).append(value)
    return settings


def local_url(url: str) -> bool:
    """Return True when Git reads `url` as a repository on this machine."""
    if url.startswith("file://"):
        return True
    if "://" in url:
        return False
    # Git reads `host:path` as SSH when a colon comes before the first slash.
    colon, slash = url.find(":"), url.find("/")
    return colon < 0 or 0 <= slash < colon


def source_origin(source: Path, origin: str | None) -> tuple[str, str]:
    """Return the upstream URL and the source refs that represent it."""
    if origin is not None:
        return origin, "refs/remotes/origin"
    return str(source), "refs/heads"


def refresh_remotes(git: GitRun, tree: Path, source: Path, origin: str | None, *, new_checkout: bool = False,
                    settings: GitSettings | None = None) -> None:
    """Keep upstream refs and saved local branches in separate remotes.

    `origin` is the source's origin URL, or None when the source has no origin.
    `settings` is a current `--local` read of `tree`. When it is None, the call reads it.
    """
    url, refs = source_origin(source, origin)
    if settings is None:
        settings = read_git_settings(git, tree, "--local")

    def references(repo: Path, *prefixes: str) -> dict[str, str]:
        listing = git(repo, "for-each-ref", "--format=%(refname) %(objectname) %(symref)", *prefixes)
        return dict(line.split(" ", 1) for line in listing.decode().splitlines())

    # A new checkout gets both ref sets in one fetch. Compare refs only on reuse.
    source_references = {} if new_checkout else references(source, refs, "refs/heads")
    current_refs = {} if new_checkout else references(tree, "refs/remotes/origin", "refs/remotes/source")
    mappings: list[str] = []
    stale_heads: list[str] = []
    for remote, remote_url, source_refs in (("origin", url, refs), ("source", str(source), "refs/heads")):
        mapping = f"+{source_refs}/*:refs/remotes/{remote}/*"
        for key, value in ((f"remote.{remote}.url", remote_url),
                           (f"remote.{remote}.fetch", f"+refs/heads/*:refs/remotes/{remote}/*")):
            if settings.get(key) != [value]:
                _ = git(tree, "config", "--replace-all", key, value)
        prefix = f"refs/remotes/{remote}/"
        expected = {name.removeprefix(source_refs + "/"): value.split()[0]
                    for name, value in source_references.items() if name.startswith(source_refs + "/")}
        current = {name.removeprefix(prefix): value.strip()
                   for name, value in current_refs.items() if name.startswith(prefix)}
        if new_checkout or current != expected:
            stale_heads.append(prefix + "HEAD")
            mappings.append(mapping)
    if stale_heads:
        # Pruning keeps symbolic HEAD refs. Remove them before copying the source's refs.
        _ = git(tree, "update-ref", "--no-deref", "--stdin",
                data="".join(f"delete {ref}\n" for ref in stale_heads).encode())
    if mappings:
        _ = git(tree, "fetch", "-q", "--prune", "--", str(source), *mappings)
