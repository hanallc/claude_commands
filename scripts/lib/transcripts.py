"""Read plain and zstd transcripts."""
from __future__ import annotations

from collections.abc import Generator, Iterable
from contextlib import contextmanager
from pathlib import Path
import os
import re
import subprocess
import tempfile
from typing import BinaryIO, cast


def uncompressed_path(path: Path) -> Path:
    return path.with_suffix("") if path.suffix == ".zst" else path


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
def stream_transcript(path: Path) -> Generator[BinaryIO]:
    """Read the transcript in order without a temporary file."""
    source = transcript_source(path)
    with source.open("rb") as stream:
        if source.suffix != ".zst":
            yield stream
            return
        with subprocess.Popen(["zstd", "-q", "-d", "-c"], stdin=stream, stdout=subprocess.PIPE) as decoder:
            assert decoder.stdout is not None
            output = cast(BinaryIO, decoder.stdout)
            yield output
            # Check the complete archive even when the caller uses only the first lines.
            while output.read(65536):
                pass
            status = decoder.wait()
            if status:
                raise OSError(f"{source}: zstd decompression failed with status {status}")


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


def compress_transcript(path: Path, output: BinaryIO) -> int:
    """Write zstd data and return its size without removing the transcript."""
    _ = subprocess.run(["zstd", "-q", "-c", "--", str(path)],
                       stdout=output, stderr=subprocess.PIPE, check=True)
    return output.tell()
