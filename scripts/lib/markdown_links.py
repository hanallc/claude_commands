"""Find text and link destinations outside Markdown code."""

from __future__ import annotations

import re
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from html import unescape
from urllib.parse import quote

_PUNCTUATION = r"[!-/:-@\[-`{-~]"
_ESCAPE = re.compile(r"\\" + _PUNCTUATION)
_BACKTICKS = re.compile("`" + "+")
_CODE_TOKEN = re.compile(_ESCAPE.pattern + r"|" + _BACKTICKS.pattern)
_LINE = re.compile(r"[^\r\n]*(?:\r\n|[\r\n]|$)")
_QUOTE = re.compile(r" {0,3}> ?")
_LIST_ITEM = re.compile(r" {0,3}(?P<marker>[-+*]|[0-9]{1,9}[.)])(?= |$)")
_HEADING = re.compile(r" {0,3}#{1,6}(?: |$)")
_SETEXT = re.compile(r" {0,3}(?:=+|-+) *$")
_THEMATIC_BREAK = re.compile(r" {0,3}(?:(?:\* *){3,}|(?:- *){3,}|(?:_ *){3,})$")
_FENCE = re.compile(r" {0,3}(?P<marker>" + "`" + r"{3,}|~{3,})(?P<tail>.*)")
_REFERENCE = re.compile(_ESCAPE.pattern + r"|&(?:#[xX][\da-fA-F]{1,6}|#[0-9]{1,7}|[a-zA-Z][a-zA-Z0-9]*);|#")


@dataclass(frozen=True)
class BlockQuote:
    """A block that continues after a quote marker."""


@dataclass(frozen=True)
class ListItem:
    """A list item that continues at its content column."""

    marker: str
    indent: int


type BlockContainer = BlockQuote | ListItem


@dataclass(frozen=True)
class ContinuedContainers:
    """The containers and columns that a line continues."""

    count: int
    columns: int


@dataclass(frozen=True)
class CodeFence:
    """The delimiter of a fenced code block."""

    marker: str

    def closes(self, line: str, columns: int) -> bool:
        match = _FENCE.fullmatch(line, columns)
        if match is None or match["tail"].strip(" "):
            return False
        return match["marker"].startswith(self.marker)


@dataclass(frozen=True)
class OutsideCodeFence:
    """The current container has no open code fence."""


@dataclass(frozen=True)
class InlineBlock:
    """Inline text with container markers masked at their original offsets."""

    offset: int
    text: str


def _indent(line: str, columns: int) -> int:
    rest = line[columns:]
    return len(rest) - len(rest.lstrip(" "))


def _continued_containers(line: str, containers: Sequence[BlockContainer]) -> ContinuedContainers:
    columns = 0
    for count, container in enumerate(containers):
        if isinstance(container, BlockQuote):
            quote_marker = _QUOTE.match(line, columns)
            if quote_marker is None:
                return ContinuedContainers(count, columns)
            columns = quote_marker.end()
        elif not line[columns:].strip(" "):
            columns = len(line)
        elif _indent(line, columns) >= container.indent:
            columns += container.indent
        else:
            return ContinuedContainers(count, columns)
    return ContinuedContainers(len(containers), columns)


def _paragraph_list_start(line: str, marker: re.Match[str]) -> bool:
    bullet = marker["marker"]
    return bool(line[marker.end():].strip(" ")) and (len(bullet) == 1 or int(bullet[:-1]) == 1)


def _start_containers(line: str, columns: int, containers: list[BlockContainer], *, paragraph: bool) -> int:
    while True:
        quote_marker = _QUOTE.match(line, columns)
        if quote_marker is not None:
            containers.append(BlockQuote())
            columns = quote_marker.end()
        else:
            marker = _LIST_ITEM.match(line, columns)
            if (marker is None or _THEMATIC_BREAK.fullmatch(line, columns) is not None
                    or paragraph and not _paragraph_list_start(line, marker)):
                return columns
            padding = _indent(line, marker.end())
            content = marker.end() + (padding if 1 <= padding <= 4 else 1)
            containers.append(ListItem(marker["marker"][-1], content - columns))
            columns = min(content, len(line))
        paragraph = False


def _opening_fence(line: str, columns: int) -> CodeFence | OutsideCodeFence:
    match = _FENCE.fullmatch(line, columns)
    if match is None:
        return OutsideCodeFence()
    marker = match["marker"]
    if marker[0] == "`" and "`" in match["tail"]:
        return OutsideCodeFence()
    return CodeFence(marker)


def _lazy_paragraph_line(line: str, columns: int, container: BlockContainer) -> bool:
    if (not line[columns:].strip(" ") or isinstance(_opening_fence(line, columns), CodeFence)
            or _QUOTE.match(line, columns) is not None or _HEADING.match(line, columns) is not None
            or _THEMATIC_BREAK.fullmatch(line, columns) is not None):
        return False
    marker = _LIST_ITEM.match(line, columns)
    if marker is None:
        return True
    return not (_paragraph_list_start(line, marker)
                or isinstance(container, ListItem) and marker["marker"][-1] == container.marker)


def _mask_container_prefix(line: str, columns: int) -> str:
    column = 0
    for index, character in enumerate(line):
        if column >= columns:
            return " " * index + line[index:]
        column += 4 - column % 4 if character == "\t" else 1
    return line


def _inline_blocks(text: str) -> Iterator[InlineBlock]:
    containers: list[BlockContainer] = []
    fence: CodeFence | OutsideCodeFence = OutsideCodeFence()
    paragraph: list[str] = []
    start = 0
    for match in _LINE.finditer(text):
        raw = match[0]
        if not raw:
            break
        line = raw.rstrip("\r\n").expandtabs(4)
        prefix = _continued_containers(line, containers)
        columns = prefix.columns
        if prefix.count < len(containers):
            if paragraph and _lazy_paragraph_line(line, columns, containers[prefix.count]):
                paragraph.append(_mask_container_prefix(raw, columns))
                continue
            if paragraph:
                yield InlineBlock(start, "".join(paragraph))
                paragraph.clear()
            del containers[prefix.count:]
            fence = OutsideCodeFence()
        if isinstance(fence, CodeFence):
            if fence.closes(line, columns):
                fence = OutsideCodeFence()
            continue

        count = len(containers)
        columns = _start_containers(line, columns, containers, paragraph=bool(paragraph))
        if len(containers) != count and paragraph:
            yield InlineBlock(start, "".join(paragraph))
            paragraph.clear()

        fence = _opening_fence(line, columns)
        heading = _HEADING.match(line, columns) is not None
        ends_paragraph = (isinstance(fence, CodeFence) or heading or not line[columns:].strip(" ")
                          or _THEMATIC_BREAK.fullmatch(line, columns) is not None
                          or bool(paragraph) and _SETEXT.fullmatch(line, columns) is not None)
        if ends_paragraph:
            if paragraph:
                yield InlineBlock(start, "".join(paragraph))
                paragraph.clear()
            if heading:
                yield InlineBlock(match.start(), _mask_container_prefix(raw, columns))
            continue
        if not paragraph:
            if _indent(line, columns) >= 4:
                continue
            start = match.start()
        paragraph.append(_mask_container_prefix(raw, columns))
    if paragraph:
        yield InlineBlock(start, "".join(paragraph))


def _outside_code_spans(text: str) -> Iterator[tuple[int, int]]:
    closing_ticks: dict[int, list[int]] = defaultdict(list)
    for run in _BACKTICKS.finditer(text):
        closing_ticks[len(run[0])].append(run.start())
    start, position = 0, 0
    while (token := _CODE_TOKEN.search(text, position)) is not None:
        position = token.end()
        if not token[0].startswith("`"):
            continue
        length = len(token[0])
        candidates = closing_ticks[length]
        index = bisect_left(candidates, position)
        if index < len(candidates):
            yield start, token.start()
            start = position = candidates[index] + length
    yield start, len(text)


def rewrite_outside_code(text: str, rewrite: Callable[[str], str]) -> str:
    """Apply the same code boundaries to each path and link form."""
    result: list[str] = []
    previous = 0
    for block in _inline_blocks(text):
        for start, end in _outside_code_spans(block.text):
            start += block.offset
            end += block.offset
            result.extend((text[previous:start], rewrite(text[start:end])))
            previous = end
    result.append(text[previous:])
    return "".join(result)


def path_and_fragment(destination: str) -> tuple[str, str]:
    """Decode Markdown punctuation and entities in the path before its fragment."""
    pieces: list[str] = []
    position = 0
    for token in _REFERENCE.finditer(destination):
        pieces.append(destination[position:token.start()])
        decoded = token[0][1:] if token[0].startswith("\\") else unescape(token[0])
        if decoded == "#":
            return "".join(pieces), destination[token.start():]
        pieces.append(decoded)
        position = token.end()
    pieces.append(destination[position:])
    return "".join(pieces), ""


def encode_path(path: str, *, angle_brackets: bool, literal: bool) -> str:
    """Encode a changed destination in its original Markdown form."""
    if literal and angle_brackets:
        return re.sub(r"[<>\\&]", lambda match: "\\" + match[0], path.replace("%", "%25"))
    return quote(path, safe="/")
