"""Find transcript folders and open plain and compressed transcripts."""
from __future__ import annotations

from collections.abc import Generator, Iterable
from contextlib import contextmanager
from pathlib import Path
import os
import re
import subprocess
import tempfile
from typing import BinaryIO


def uncompressed_path(path: Path) -> Path:
    return path.with_suffix("") if path.suffix == ".zst" else path


def config_dirs() -> list[Path]:
    """List Claude config folders, including the configured override."""
    directories = {path for path in Path.home().glob(".claude*") if path.is_dir()}
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        directories.add(Path(override).expanduser())
    return sorted(directories)


def codex_transcript_homes(extra_homes: Iterable[Path] = ()) -> list[Path]:
    """List transcript homes with the configured home first, without login filtering."""
    root = Path.home()
    numbered = sorted((home for home in root.glob(".codex*")
                       if re.fullmatch(r"\.codex[0-9]+", home.name) and home.is_dir()),
                      key=lambda home: int(home.name.removeprefix(".codex")))
    homes = [Path(configured).expanduser()] if (configured := os.environ.get("CODEX_HOME")) else []
    homes.extend(home.expanduser() for home in extra_homes)
    homes.extend((root / ".codex", *numbered))
    distinct: dict[Path, Path] = {}
    for home in homes:
        _ = distinct.setdefault(home.resolve(), home)
    return list(distinct.values())


def transcript_paths(root: Path, pattern: str = "**/*.jsonl") -> list[Path]:
    """Find each logical transcript once, with the plain file preferred."""
    found: dict[Path, Path] = {}
    for suffix in (".zst", ""):
        for path in root.glob(pattern + suffix):
            found[uncompressed_path(path)] = path
    return sorted(found.values(), key=str)


def claude_transcript_candidates(config_dir: Path, session_id: str) -> Generator[Path]:
    """Yield one logical path per project. Report directory read errors to the caller."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
        raise ValueError("invalid Claude session ID")
    for project in (config_dir / "projects").iterdir():
        yield project / f"{session_id}.jsonl"


def transcript_stems(root: Path) -> list[str]:
    """Name each plain or compressed transcript under root once, without a path for each file."""
    stems: dict[str, None] = {}
    for _, _, names in os.walk(root):
        for name in names:
            if (logical := name.removesuffix(".zst")).endswith(".jsonl"):
                stems[logical.removesuffix(".jsonl")] = None
    return list(stems)


def distinct_files(paths: Iterable[Path]) -> list[Path]:
    """Merge aliases in a small list of transcript matches."""
    return sorted({path.resolve() for path in paths})


def transcript_source(path: Path) -> Path:
    if path.suffix != ".zst" and not path.exists():
        compressed = path.with_suffix(path.suffix + ".zst")
        if compressed.exists():
            return compressed
    return path


@contextmanager
def open_transcript(path: Path) -> Generator[BinaryIO]:
    """Keep decompression outside memory and permit existing readers to seek."""
    source = transcript_source(path)
    if source.suffix != ".zst":
        with source.open("rb") as stream:
            yield stream
        return
    with tempfile.TemporaryFile() as stream:
        try:
            _ = subprocess.run(["zstd", "-q", "-d", "-c", "--", str(source)],
                               stdout=stream, stderr=subprocess.PIPE, check=True)
        except subprocess.CalledProcessError as error:
            if not source.exists():
                raise FileNotFoundError(source) from error
            raise
        _ = stream.seek(0)
        yield stream


def compressed_transcript_lines(path: Path) -> Generator[bytes]:
    """Read compressed transcript lines in order through a pipe."""
    command = ["zstd", "-q", "-d", "-c"]
    with path.open("rb") as source, subprocess.Popen(
        command, stdin=source, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    ) as decoder:
        assert decoder.stdout is not None
        with decoder.stdout:
            yield from decoder.stdout
        if status := decoder.wait():
            raise subprocess.CalledProcessError(status, command)


def transcript_lines(path: Path) -> Generator[bytes]:
    """Yield plain or compressed transcript lines without writing a file."""
    source = transcript_source(path)
    if source.suffix == ".zst":
        yield from compressed_transcript_lines(source)
        return
    with source.open("rb") as stream:
        yield from stream


def compress_transcript(path: Path, output: BinaryIO) -> int:
    """Write zstd data and return its size without removing the transcript."""
    _ = subprocess.run(["zstd", "-q", "-c", "--", str(path)],
                       stdout=output, stderr=subprocess.PIPE, check=True)
    return output.tell()
