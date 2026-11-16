"""Write the Python facts for the Rust reader's differential tests."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
import re
import subprocess
import sys
from typing import BinaryIO, NotRequired, Protocol, TypedDict, cast

from scripts.style import declarations, style_footprint, style_plan, style_ledger


type Json = str | int | bool | None | list[Json] | dict[str, Json]

# The oracle calls the Python source function without adding another public ledger API.
python_code_identity = cast(Callable[[bytes], style_ledger.RustCode | style_ledger.NotTokenized],
                            getattr(style_ledger, "_rust_code"))


class ModuleMove(TypedDict):
    old: str
    new: str
    file_module: dict[str, list[str]] | str
    parent: list[str]
    library: dict[str, str] | str


class ModuleQuery(TypedDict):
    blob: str
    path: str
    moves: list[ModuleMove]


class Request(TypedDict):
    repository: str
    commits: list[str]
    facts: list[str]
    module_sites: list[ModuleQuery]
    extensions: NotRequired[list[str]]
    blobs: NotRequired[list[str]]
    trees: NotRequired[bool]
    name_word_class: NotRequired[str]


class BodyWords(Protocol):
    @property
    def open(self) -> int: ...

    @property
    def close(self) -> int: ...


class ItemSpan(Protocol):
    @property
    def keyword(self) -> int: ...

    @property
    def end(self) -> int: ...

    @property
    def body(self) -> object: ...


def position_value(position: declarations.ImplPosition) -> Json:
    match position:
        case declarations.FileLevelImpl():
            return "file_level"
        case declarations.BlockScopedImpl(header_word=word):
            return {"block_scoped": word}
        case declarations.UnprovedImplPosition():
            return "unproved"


def membership_value(membership: declarations.TraitMembership | declarations.TypeMembership) -> Json:
    match membership:
        case declarations.NoImplementedTrait() | declarations.NoImplementedType():
            return "none"
        case declarations.UnreadableImplementedTrait() | declarations.UnreadableImplementedType():
            return "unreadable"
        case declarations.ImplementedTrait() | declarations.ImplementedType():
            return {"path": list(membership.path), "arguments": list(membership.arguments),
                    "position": position_value(membership.position)}


def span_value(span: ItemSpan) -> Json:
    body: Json = "none"
    if hasattr(span.body, "open"):
        words = cast(BodyWords, span.body)
        body = {"open": words.open, "close": words.close}
    return {"keyword": span.keyword, "end": span.end, "body": body}


def declaration_value(file: declarations.DeclaredFile) -> Json:
    items: list[Json] = []
    for item in file.declared_items:
        owner = item.declaration.owner
        items.append({
            "keyword": item.declaration.keyword, "name": item.declaration.name,
            "owner": {"impl_or_trait": owner.name} if isinstance(owner, declarations.ImplOrTraitItem) else "module",
            "keyword_line": item.keyword_line, "first_line": item.first_line, "last_line": item.last_line,
            "implemented_trait": membership_value(item.implemented_trait),
            "import_position": position_value(item.import_position),
            "implemented_type": membership_value(item.implemented_type),
        })
    spans = cast(tuple[ItemSpan, ...], getattr(file, "_spans"))
    others = cast(tuple[ItemSpan, ...], getattr(file, "_others"))
    return {"items": items, "spans": [span_value(span) for span in spans],
            "others": [span_value(span) for span in others]}


def name_site_value(file: declarations.DeclaredFile) -> Json:
    readings: list[Json] = []
    for item in file.declared_items:
        body = declarations.item_body(file, item)
        if isinstance(body, declarations.UnreadableBody):
            readings.append("unreadable")
            continue
        sites = declarations.name_sites(body, item.declaration.name)
        readings.append("unreadable" if isinstance(sites, declarations.UnreadableBody)
                        else {"sites": [[start, end] for start, end in sites.sites]})
    return readings


def body_token_value(file: declarations.DeclaredFile, text: str) -> Json:
    """Match the normalized body tokens that the declaration reader shares."""
    normalized = "\n".join(file.source_lines)
    original = text.startswith(normalized) and not text[len(normalized):].strip("\n")
    reading = style_footprint.tokenized_text(text if original else normalized)
    if any(token.kind == "unknown" for token in reading.tokens):
        return "unreadable"
    if original:
        return "original"
    return [{"value": token.value, "kind": token.kind, "line": line, "start": start}
            for token, line, start in zip(reading.tokens, reading.lines, reading.starts, strict=True)]


def module_scan_value(reading: style_plan.ModuleDeclarationText) -> Json:
    items: list[Json] = []
    for item in reading.declarations:
        path: Json = "default"
        if isinstance(item.path, style_plan.LiteralModulePath):
            path = {"literal": item.path.value}
        elif isinstance(item.path, style_plan.UncertainDeclarationPath):
            path = "uncertain"
        items.append({"name": item.name, "scope": [scope.name for scope in item.scope], "path": path})
    return {"declarations": items}


def module_sites_value(text: str, query: ModuleQuery) -> Json:
    try:
        spans = style_footprint.token_spans(text)
    except style_footprint.UnreadableTokenSpans:
        unreadable: list[Json] = ["no_renamed_text"]
        return unreadable * len(query["moves"])
    plain = style_plan.plain_words(spans)
    readings: list[Json] = []
    for move in query["moves"]:
        context = move["file_module"]
        module = tuple(context["crate"]) if isinstance(context, dict) else style_footprint.OutsideCrate()
        library_data = move["library"]
        library = style_plan.Library(library_data["name"]) if isinstance(library_data, dict) else style_plan.NoLibrary()
        scan = style_plan.RenamedModuleScan(query["path"], plain, module, tuple(move["parent"]),
                                            move["old"], move["new"], library)
        renamed = style_plan.module_renames_from_scans(query["path"], text, spans, (scan,))
        if isinstance(renamed, style_plan.NoRenamedText):
            readings.append("no_renamed_text")
        else:
            readings.append("uncertain_module_path" if isinstance(renamed, style_plan.UncertainModulePath)
                            else {"module_sites": renamed.module_sites})
    return readings


def blob_ids(repository: Path, commits: list[str]) -> list[str]:
    blobs: set[str] = set()
    for commit in commits:
        tree = subprocess.run(["git", "-C", str(repository), "ls-tree", "-rz", commit],
                              check=True, capture_output=True).stdout
        for entry in tree.split(b"\0"):
            if not entry:
                continue
            header, path = entry.split(b"\t", 1)
            mode, kind, blob = header.split()
            if kind == b"blob" and mode in (b"100644", b"100755") and path.endswith(b".rs"):
                blobs.add(blob.decode("ascii"))
    return sorted(blobs)


def requested_blobs(request: Request) -> set[str]:
    """Select regular blobs for the requested file extensions and explicit ids."""
    extensions = request.get("extensions", ["rs"])
    if extensions == ["rs"]:
        return set(blob_ids(Path(request["repository"]), request["commits"])) | set(request.get("blobs", []))
    selected = set(request.get("blobs", []))
    suffixes = tuple(("." + extension).encode() for extension in extensions)
    for commit in request["commits"]:
        tree = subprocess.run(["git", "-C", request["repository"], "ls-tree", "-rz", commit],
                              check=True, capture_output=True).stdout
        for entry in tree.split(b"\0"):
            if not entry:
                continue
            header, path = entry.split(b"\t", 1)
            mode, kind, blob = header.split()
            if kind == b"blob" and mode in (b"100644", b"100755") and path.endswith(suffixes):
                selected.add(blob.decode("ascii"))
    return selected


def emit_trees(request: Request) -> None:
    """Use Git as the oracle for recursive tree entries and their order."""
    for commit in request["commits"]:
        tree = subprocess.run(["git", "-C", request["repository"], "rev-parse", commit + "^{tree}"],
                              check=True, capture_output=True).stdout.decode().strip()
        listing = subprocess.run(["git", "-C", request["repository"], "ls-tree", "-rzt", "--full-tree", commit],
                                 check=True, capture_output=True).stdout.decode()
        entries: list[list[str]] = []
        for entry in listing.split("\0"):
            if entry:
                metadata, path = entry.split("\t", 1)
                entries.append([path, *metadata.split()])
        print(json.dumps({"commit": commit, "tree": tree, "entries": entries}))


def emit(blob: str, fact: str, value: Json, *, unreadable: str = "", path: str = "") -> None:
    line: dict[str, Json] = {"blob": blob, "fact": fact}
    line["unreadable" if unreadable else "value"] = unreadable if unreadable else value
    if path:
        line["path"] = path
    print(json.dumps(line, ensure_ascii=True, sort_keys=True))


def answer(request: Request) -> None:
    repository = Path(request["repository"])
    queries = request.get("module_sites", [])
    queried: dict[str, list[ModuleQuery]] = {}
    for query in queries:
        queried.setdefault(query["blob"], []).append(query)
    rust_blobs = requested_blobs(request)
    if request.get("trees", False):
        emit_trees(request)
    with subprocess.Popen[bytes](["git", "-C", str(repository), "cat-file", "--batch"],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE) as process:
        if process.stdin is None or process.stdout is None:
            raise RuntimeError("the Git batch reader has no pipes")
        input_stream = cast(BinaryIO, process.stdin)
        output_stream = cast(BinaryIO, process.stdout)
        for blob in sorted(rust_blobs | queried.keys()):
            request_line = (blob + "\n").encode("ascii")
            if input_stream.write(request_line) != len(request_line):
                raise RuntimeError("the Git batch reader rejected a request")
            input_stream.flush()
            header = output_stream.readline().split()
            if len(header) == 2 and header[1] == b"missing":
                for fact in sorted(set(request["facts"])) if blob in rust_blobs else []:
                    emit(blob, fact, None, unreadable="missing_blob")
                for query in queried.get(blob, []):
                    emit(blob, "module_sites", None, unreadable="missing_blob", path=query["path"])
                continue
            if len(header) != 3 or header[1] != b"blob":
                raise ValueError(f"cannot read blob {blob}")
            data = output_stream.read(int(header[2]))
            separator = output_stream.read(1)
            if separator != b"\n":
                raise ValueError("invalid Git batch separator")
            facts = sorted(set(request["facts"])) if blob in rust_blobs else []
            try:
                text = data.decode("utf-8")
                valid_utf8 = True
            except UnicodeError:
                text = data.decode("utf-8", errors="replace")
                valid_utf8 = False
            file = declarations.read_declarations(text) if {"declarations", "name_sites"} & set(facts) else None
            for fact in facts:
                value: Json
                if not valid_utf8 and (fact in {"lines", "rust_code"}
                                       or fact == "names" and request.get("name_word_class") != "ascii"):
                    emit(blob, fact, None, unreadable="not_utf8")
                    continue
                match fact:
                    case "rust_code":
                        reading_code = python_code_identity(data)
                        if isinstance(reading_code, style_ledger.NotTokenized):
                            emit(blob, fact, None, unreadable="token_spans")
                            continue
                        value = reading_code.digest.hex()
                    case "tokens":
                        reading = style_footprint.tokenized_text(text)
                        value = [{"value": token.value, "kind": token.kind, "line": line, "start": start}
                                 for token, line, start in zip(reading.tokens, reading.lines, reading.starts, strict=True)]
                    case "lines":
                        value = len(text.replace("\r\n", "\n").replace("\r", "\n").splitlines())
                    case "names":
                        if request.get("name_word_class") == "ascii":
                            raw_words = cast(list[bytes], re.findall(rb"[A-Za-z0-9_]+", data))
                            value = [word.decode("ascii") for word in sorted(set(raw_words))]
                        else:
                            words = subprocess.run(
                                ["git", "-C", str(repository), "grep", "-a", "-h", "-o", "-E",
                                 "[[:alnum:]_]+", blob], capture_output=True,
                            )
                            if words.returncode not in (0, 1):
                                raise RuntimeError(words.stderr.decode(errors="replace"))
                            posix_words: list[Json] = [word for word in sorted(set(words.stdout.decode().splitlines()))]
                            value = posix_words
                    case "module_scan":
                        scan = style_plan.module_declaration_text(text)
                        if isinstance(scan, style_plan.UnreadableDeclarationText):
                            emit(blob, fact, None, unreadable="token_spans")
                            continue
                        value = module_scan_value(scan)
                    case "declarations" | "name_sites":
                        if file is None:
                            raise RuntimeError("the declaration reading is missing")
                        if fact == "declarations":
                            declared = cast(dict[str, Json], declaration_value(file))
                            declared["body_tokens"] = body_token_value(file, text)
                            value = declared
                        else:
                            value = name_site_value(file)
                    case _:
                        raise ValueError(f"unknown fact {fact}")
                emit(blob, fact, value)
            for query in queried.get(blob, []):
                emit(blob, "module_sites", module_sites_value(text, query), path=query["path"])
            style_footprint.tokenized_text.cache_clear()
        input_stream.close()
        if process.wait() != 0:
            raise RuntimeError("the Git batch reader failed")


def main() -> None:
    request = cast(Request, json.load(sys.stdin))
    answer(request)


if __name__ == "__main__":
    main()
