#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""git clean filter: drop generated backlog fields from issue frontmatter.

Reads a note on stdin and writes it to stdout with the ``backlog_score`` and
``backlog_rank`` lines removed from the leading YAML frontmatter, so the derived
ranking never enters git history. The working tree keeps the fields (Obsidian's
Base views sort on ``backlog_rank``); only the committed blob is normalized.

Anything that is not a frontmatter note, or that lacks these fields, passes
through byte-for-byte.

With ``--process`` it is a Git long-running filter (``filter.<driver>.process``):
one process cleans every file of a Git command, so a large add starts Python once.
"""

from __future__ import annotations

import sys
from typing import BinaryIO

GENERATED_KEYS = ("backlog_score:", "backlog_rank:")
PROCESS_FLAG = "--process"
FLUSH = b"0000"
PACKET_DATA_LIMIT = 65516


class ProtocolError(Exception):
    """Git sent bytes that do not follow the long-running filter protocol."""


def strip_generated(data: bytes) -> bytes:
    text = data.decode("utf-8")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return data
    closing: int | None = None
    for index in range(1, len(lines)):
        if lines[index].rstrip("\r\n") == "---":
            closing = index
            break
    if closing is None:
        return data
    kept: list[str] = [
        line
        for index, line in enumerate(lines)
        if not (1 <= index < closing and line.startswith(GENERATED_KEYS))
    ]
    return "".join(kept).encode("utf-8")


def read_exact(stream: BinaryIO, size: int) -> bytes:
    data = stream.read(size)
    if len(data) != size:
        raise ProtocolError("Git closed the filter input inside a packet.")
    return data


def read_packet(stream: BinaryIO, *, request_start: bool = False) -> bytes | None:
    """Return one packet's data, or None for a flush packet.

    An end of input is normal only at the start of a request; it raises EOFError there.
    """
    header = stream.read(4)
    if not header and request_start:
        raise EOFError
    if len(header) != 4:
        raise ProtocolError("Git closed the filter input inside a packet header.")
    size = int(header, 16)
    if size == 0:
        return None
    if size < 4:
        raise ProtocolError(f"Git sent a packet with the reserved length {size}.")
    return read_exact(stream, size - 4)


def read_text_list(stream: BinaryIO, *, request_start: bool = False) -> list[str]:
    lines: list[str] = []
    while (packet := read_packet(stream, request_start=request_start and not lines)) is not None:
        # A path name can hold any bytes; keep them without a decode error.
        lines.append(packet.decode("utf-8", "surrogateescape").removesuffix("\n"))
    return lines


def read_content(stream: BinaryIO) -> bytes:
    chunks: list[bytes] = []
    while (packet := read_packet(stream)) is not None:
        chunks.append(packet)
    return b"".join(chunks)


def write_packet(stream: BinaryIO, data: bytes) -> None:
    _ = stream.write(f"{len(data) + 4:04x}".encode() + data)


def write_text_list(stream: BinaryIO, lines: list[str]) -> None:
    for line in lines:
        write_packet(stream, f"{line}\n".encode())
    _ = stream.write(FLUSH)
    stream.flush()


def write_content(stream: BinaryIO, data: bytes) -> None:
    for start in range(0, len(data), PACKET_DATA_LIMIT):
        write_packet(stream, data[start:start + PACKET_DATA_LIMIT])
    _ = stream.write(FLUSH)


def handshake(source: BinaryIO, sink: BinaryIO) -> None:
    welcome = read_text_list(source)
    if not welcome or welcome[0] != "git-filter-client" or "version=2" not in welcome[1:]:
        raise ProtocolError("Git did not offer filter protocol version 2.")
    write_text_list(sink, ["git-filter-server", "version=2"])
    if "capability=clean" not in read_text_list(source):
        raise ProtocolError("Git did not offer the clean capability.")
    write_text_list(sink, ["capability=clean"])


def serve(source: BinaryIO, sink: BinaryIO) -> None:
    """Clean each file Git sends until Git closes the input."""
    handshake(source, sink)
    while True:
        try:
            request = read_text_list(source, request_start=True)
        except EOFError:
            return
        # The handshake offers only clean, so Git sends no other command.
        if "command=clean" not in request:
            raise ProtocolError(f"Git sent a request without command=clean: {request!r}")
        content = read_content(source)
        try:
            cleaned = strip_generated(content)
        except UnicodeDecodeError:
            write_text_list(sink, ["status=error"])
            continue
        write_text_list(sink, ["status=success"])
        write_content(sink, cleaned)
        # An empty final list keeps the success status.
        write_text_list(sink, [])


def main() -> int:
    arguments = sys.argv[1:]
    if arguments == [PROCESS_FLAG]:
        serve(sys.stdin.buffer, sys.stdout.buffer)
        return 0
    if arguments:
        print(f"usage: strip_generated.py [{PROCESS_FLAG}]", file=sys.stderr)
        return 2
    _ = sys.stdout.buffer.write(strip_generated(sys.stdin.buffer.read()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
