#!/usr/bin/env python3
"""Find changes to existing tests and check their landing records."""
from __future__ import annotations

try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import ast
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass
from datetime import datetime
from difflib import SequenceMatcher
from enum import StrEnum
from functools import cache
import hashlib
from importlib import import_module
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tokenize
from typing import NamedTuple, cast

from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.showrunner_state import state_root


class BehaviorCheckError(RuntimeError):
    """The test changes or their review record could not be read."""


class NoQuestionDraft(StrEnum):
    OMITTED = "no question file supplied"


class GitRange(NamedTuple):
    repo: Path
    base: str
    commit: str

    def git(self, *args: str, timeout: float | None = None) -> bytes:
        try:
            result = subprocess.run(["git", "-C", str(self.repo), *args],
                                    capture_output=True, check=False, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise BehaviorCheckError(f"git {args[0]} timed out after {error.timeout:g} seconds") from error
        if result.returncode:
            detail = os.fsdecode(result.stderr).strip()
            raise BehaviorCheckError(detail or f"git {args[0]} exited with status {result.returncode}")
        return result.stdout

    def pinned(self) -> GitRange:
        base = self.git("rev-parse", "--verify", "--end-of-options", f"{self.base}^{{commit}}")
        commit = self.git("rev-parse", "--verify", "--end-of-options", f"{self.commit}^{{commit}}")
        return GitRange(self.repo, base.decode("ascii").strip(), commit.decode("ascii").strip())

    def origin_identity(self, timeout: float | None) -> str:
        remote = os.fsdecode(self.git("remote", "get-url", "origin", timeout=timeout)).strip()
        if ":" not in remote:
            root = os.fsdecode(self.git("rev-parse", "--show-toplevel", timeout=timeout)).rstrip("\n")
            return str((Path(root) / remote).resolve())
        return remote

    def command(self, action: str, *, production: str = "", question: Path | NoQuestionDraft = NoQuestionDraft.OMITTED) -> str:
        scripts = Path(__file__).resolve().parents[1]
        command = [str(scripts / "lib/py"), str(scripts / "production/behavior_check.py"), action,
                   "--repo", str(self.repo.resolve()), "--base", self.base, "--commit", self.commit]
        if production:
            command.extend(("--production", production))
        if isinstance(question, Path):
            command.extend(("--question", str(question.resolve())))
        return shlex.join(command)


class TestSpan(NamedTuple):
    name: str
    first: int
    last: int
    decorators: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True, order=True)
class TestChange:
    test: str
    old: str
    new: str


class NoChangedTests(StrEnum):
    EMPTY = "no altered or removed tests"


class ChangedFile(NamedTuple):
    path: str
    old_mode: str
    new_mode: str
    old_blob: str
    new_blob: str


@dataclass(frozen=True)
class ChangedTests:
    tests: tuple[TestChange, ...]
    repository: str
    files: tuple[ChangedFile, ...]

    @property
    def key(self) -> str:
        changes = sorted((change.test, change.old, change.new) for change in self.tests)
        fields = (self.repository, sorted(self.files), changes)
        return hashlib.sha256(json.dumps(fields, separators=(",", ":")).encode()).hexdigest()


def python_tests(nodes: Iterable[ast.stmt], prefix: str = "",
                 decorators: tuple[tuple[int, int], ...] = ()) -> Iterator[TestSpan]:
    for node in nodes:
        if isinstance(node, ast.ClassDef):
            inherited = tuple((item.lineno - 1, item.end_lineno or item.lineno) for item in node.decorator_list)
            yield from python_tests(node.body, prefix + node.name + ".", decorators + inherited)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            first = min((node.lineno, *(item.lineno for item in node.decorator_list)))
            assert node.end_lineno is not None
            yield TestSpan(prefix + node.name, first - 1, node.end_lineno, decorators)
        elif isinstance(node, (ast.If, ast.With, ast.Try, ast.TryStar)):
            children = (child for child in ast.iter_child_nodes(node) if isinstance(child, ast.stmt))
            yield from python_tests(children, prefix, decorators)


_RUST_TOKEN = re.compile(
    r'\s+|//[^\r\n]*|/\*|(?:br|cr|r)(?P<hashes>\#*)"(?s:.*?)"(?P=hashes)' +
    r'|"(?:[^"\\]|\\.)*"|\x27(?:[^\x27\\\n]|\\(?:u\{[^}]+\}|x[\da-fA-F]{2}|.))\x27' +
    r'|(?:r\#)?\w+|::|[^\s]', re.DOTALL,
)
_RUST_COMMENT = re.compile(r'/\*|\*/')


class RustToken(NamedTuple):
    words: str
    offset: int


def rust_tokens(source: str) -> list[RustToken]:
    tokens: list[RustToken] = []
    position = 0
    while match := _RUST_TOKEN.match(source, position):
        words = match[0]
        position = match.end()
        if words == "/*":
            depth = 1
            for comment in _RUST_COMMENT.finditer(source, position):
                depth += 1 if comment[0] == "/*" else -1
                if depth == 0:
                    position = comment.end()
                    break
            if depth:
                break
        elif not words.startswith("//") and not words.isspace():
            tokens.append(RustToken(words, match.start()))
    return tokens


def rust_pairs(tokens: list[RustToken]) -> dict[int, int]:
    closing = {"(": ")", "[": "]", "{": "}"}
    stack: list[int] = []
    pairs: dict[int, int] = {}
    for index, token in enumerate(tokens):
        if token.words in closing:
            stack.append(index)
        elif stack and closing[tokens[stack[-1]].words] == token.words:
            pairs[stack.pop()] = index
    return pairs


_RUST_TEST = re.compile(r"(?:\w+::)*test(?:\(|$)")


def rust_test_attribute(tokens: list[RustToken], pairs: dict[int, int], first: int, end: int) -> bool:
    position = first
    while position < end and tokens[position].words != ",":
        position = pairs.get(position, position) + 1
    attribute = "".join(part.words for part in tokens[first:position])
    if _RUST_TEST.match(attribute):
        return True
    if (first + 1 < position and tokens[first].words == "cfg_attr"
            and tokens[first + 1].words == "(" and pairs.get(first + 1) == position - 1):
        predicate = first + 2
        while predicate < position - 1 and tokens[predicate].words != ",":
            predicate = pairs.get(predicate, predicate) + 1
        if rust_test_attribute(tokens, pairs, predicate + 1, position - 1):
            return True
    return position + 1 < end and rust_test_attribute(tokens, pairs, position + 1, end)


class RustModule(NamedTuple):
    end: int
    name: str
    attributes: tuple[tuple[int, int], ...]


def rust_tests(source: str) -> Iterator[TestSpan]:
    tokens = rust_tokens(source)
    pairs = rust_pairs(tokens)
    scopes: list[RustModule] = []
    index = 0
    first_attribute = -1
    attribute_end = -1
    has_test = False
    while index < len(tokens):
        while scopes and scopes[-1][0] < index:
            _ = scopes.pop()
        token = tokens[index]
        words = token.words
        if words == "#" and index + 1 in pairs and tokens[index + 1].words == "[":
            end = pairs[index + 1]
            if first_attribute < 0:
                first_attribute = token.offset
            attribute_end = tokens[end].offset
            has_test = has_test or rust_test_attribute(tokens, pairs, index + 2, end)
            index = end + 1
            continue
        if words == "mod" and index + 2 in pairs and tokens[index + 2].words == "{":
            attributes: list[tuple[int, int]] = []
            if first_attribute >= 0:
                attributes.append((source.count("\n", 0, first_attribute), source.count("\n", 0, attribute_end) + 1))
            body = index + 3
            next_item = body
            while (next_item + 2 in pairs
                   and [part.words for part in tokens[next_item:next_item + 3]] == ["#", "!", "["]):
                next_item = pairs[next_item + 2] + 1
            if next_item > body:
                attributes.append((source.count("\n", 0, tokens[body].offset),
                                   source.count("\n", 0, tokens[next_item - 1].offset) + 1))
            scopes.append(RustModule(pairs[index + 2], tokens[index + 1].words, tuple(attributes)))
            first_attribute = -1
            has_test = False
            index = next_item
            continue
        if has_test and words == "fn" and index + 1 < len(tokens):
            name = "::".join([*(scope.name for scope in scopes), tokens[index + 1].words])
            body = index + 2
            while body < len(tokens) and tokens[body].words not in ("{", ";"):
                body = pairs.get(body, body) + 1
            if body in pairs and tokens[body].words == "{":
                end = pairs[body]
                yield TestSpan(name, source.count("\n", 0, first_attribute),
                               source.count("\n", 0, tokens[end].offset) + 1,
                               tuple(attribute for scope in scopes for attribute in scope.attributes))
                index = end
            has_test = False
            first_attribute = -1
        elif words in (";", "{", "}"):
            has_test = False
            first_attribute = -1
        index += 1


def is_test_file(name: str) -> bool:
    path = Path(name)
    return path.suffix == ".rs" or (
        path.suffix == ".py"
        and (path.name.startswith("test") or path.stem.endswith("_test") or "tests" in path.parts[:-1])
    )


def test_lines(path: str, blob: bytes) -> tuple[list[str], list[TestSpan]]:
    if path.endswith(".rs"):
        source = blob.decode("utf-8")
        return list(io.StringIO(source, newline="")), list(rust_tests(source))
    encoding, _ = tokenize.detect_encoding(io.BytesIO(blob).readline)
    source = blob.decode(encoding)
    return list(io.StringIO(source, newline="")), list(python_tests(ast.parse(blob, filename=path).body))


def test_blobs(change: GitRange, rev: str, paths: list[str], timeout: float | None) -> dict[str, str]:
    entries = change.git("--literal-pathspecs", "ls-tree", "--full-tree", "-r", "-z", rev, "--", *paths, timeout=timeout)
    blobs: dict[str, str] = {}
    for entry in entries.split(b"\0"):
        if entry:
            meta, path = entry.split(b"\t", 1)
            mode, _, object_id = meta.split()
            if mode in (b"100644", b"100755"):
                blobs[os.fsdecode(path)] = object_id.decode("ascii")
    return blobs


def tests_on_changed_lines(spans: list[TestSpan], ranges: list[tuple[int, int]]) -> set[str]:
    return {span.name for span in spans
            if any(start < last and first < stop for first, last in ranges
                   for start, stop in (*span.decorators, (span.first, span.last)))}


def test_texts(path: str, lines: list[str], spans: list[TestSpan], touched: set[str]) -> dict[str, str]:
    tests: dict[str, str] = {}
    for span in spans:
        if span.name in touched:
            name = f"{path}::{span.name}"
            if name in tests:
                raise BehaviorCheckError(f"more than one definition of {name}")
            included = {line for start, stop in (*span.decorators, (span.first, span.last))
                        for line in range(start, stop)}
            tests[name] = "".join(lines[line] for line in sorted(included))
    return tests


class TestPaths(NamedTuple):
    before: str
    after: str


def test_paths(change: GitRange, paths: list[str], timeout: float | None) -> list[TestPaths]:
    names = change.git("diff", "--name-status", "--find-renames", "--no-ext-diff", "--no-relative",
                       "--no-color", "-z", change.base, change.commit, "--", timeout=timeout)
    fields = iter(os.fsdecode(names).removesuffix("\0").split("\0"))
    candidates = set(paths)
    pairs: list[TestPaths] = []
    for status in fields:
        old = next(fields)
        if status.startswith("R"):
            new = next(fields)
            if old in candidates or new in candidates:
                pairs.append(TestPaths(old, new))
        elif old in candidates:
            pairs.append(TestPaths(old, old))
    return pairs


def changed_tests(change: GitRange, *, timeout: float | None = None) -> ChangedTests | NoChangedTests:
    """Compare test line ranges across the exact trees."""
    raw = change.git("diff", "--raw", "--no-abbrev", "--no-renames", "--no-ext-diff", "--no-relative", "--no-color",
                     "--ignore-submodules=none", "-z", change.base, change.commit, "--", timeout=timeout)
    fields = iter(os.fsdecode(raw).removesuffix("\0").split("\0")) if raw else iter(())
    files: list[ChangedFile] = []
    for entry in fields:
        old_mode, new_mode, old_blob, new_blob, _ = entry.removeprefix(":").split()
        files.append(ChangedFile(next(fields), old_mode, new_mode, old_blob, new_blob))
    paths = [file.path for file in files if is_test_file(file.path)]
    if not paths:
        return NoChangedTests.EMPTY
    pairs = test_paths(change, paths, timeout)
    before = test_blobs(change, change.base, sorted({pair.before for pair in pairs}), timeout)
    after = test_blobs(change, change.commit, sorted({pair.after for pair in pairs}), timeout)
    old: dict[str, str] = {}
    new: dict[str, str] = {}
    for old_path, new_path in pairs:
        if before.get(old_path) == after.get(new_path):
            continue
        try:
            left, left_spans = test_lines(old_path, change.git("cat-file", "blob", before[old_path], timeout=timeout)) if old_path in before else ([], [])
            right, right_spans = test_lines(new_path, change.git("cat-file", "blob", after[new_path], timeout=timeout)) if new_path in after else ([], [])
        except (UnicodeError, SyntaxError) as error:
            raise BehaviorCheckError(f"{old_path}: {error}") from error
        edits = [edit for edit in SequenceMatcher(None, left, right).get_opcodes() if edit[0] != "equal"]
        removed = tests_on_changed_lines(left_spans, [(a, b) for _, a, b, _, _ in edits])
        added = tests_on_changed_lines(right_spans, [(a, b) for _, _, _, a, b in edits])
        # A hunk can touch only one side of a test, such as an inserted assertion.
        touched = removed | added
        old.update(test_texts(old_path, left, left_spans, touched))
        new.update(test_texts(old_path, right, right_spans, touched))
    inserted = Counter(text for test, text in new.items() if test not in old)
    edits_to_tests: list[TestChange] = []
    for test, text in sorted(old.items()):
        if test not in new and inserted[text]:
            inserted[text] -= 1
        elif new.get(test) != text:
            edits_to_tests.append(TestChange(test, text, new.get(test, "")))
    if not edits_to_tests:
        return NoChangedTests.EMPTY
    return ChangedTests(tuple(edits_to_tests), change.origin_identity(timeout), tuple(files))


class TestHistoryCommit(NamedTuple):
    parents: list[str]
    subject: str

    def phase_author(self) -> str:
        if len(self.parents) < 2:
            return ""
        merge = re.fullmatch(r"Merge (\S+) phase (\S+) \([0-9a-fA-F]{7,64}\) into \S+", self.subject)
        return f"Ask {merge[1]} (phase {merge[2]})" if merge else ""


def describe_tip(change: GitRange) -> str:
    subject = change.git("show", "-s", "--format=%s", "--no-notes", "--no-show-signature", change.commit, "--")
    return f"{change.commit} {subject.decode('utf-8', errors='replace')}".rstrip()


def question_authors(change: GitRange, tests: ChangedTests, findings: set[str]) -> dict[str, str]:
    """Trace each finding to the commit that supplied the final test text."""
    rows = change.git("log", "--format=%H%x00%P%x00%s", "-z", "--no-notes", "--no-show-signature",
                      "--encoding=UTF-8", f"{change.base}..{change.commit}", "--")
    fields = iter(rows.decode("utf-8", errors="replace").removesuffix("\0").split("\0")) if rows else iter(())
    history = {commit: TestHistoryCommit(next(fields).split(), next(fields)) for commit in fields}
    if change.commit not in history:
        return dict.fromkeys(findings, describe_tip(change))
    pairs = test_paths(change, [file.path for file in tests.files if is_test_file(file.path)], None)
    pairs.sort(key=lambda pair: -len(pair.before))

    @cache
    def blob_at(commit: str, path: str) -> str:
        return test_blobs(change, commit, [path], None).get(path, "")

    @cache
    def tests_in_blob(path: str, blob: str) -> tuple[list[str], list[TestSpan]]:
        return test_lines(path, change.git("cat-file", "blob", blob))

    @cache
    def parent_path(parent: str, commit: str, path: str) -> str:
        if blob_at(parent, path) or not blob_at(commit, path):
            return path
        pairs = test_paths(GitRange(change.repo, parent, commit), [path], None)
        return next((pair.before for pair in pairs if pair.after == path), path)

    def has_test_text(commit: str, path: str, name: str, text: str) -> bool:
        blob = blob_at(commit, path)
        if not blob:
            return not text
        try:
            lines, spans = tests_in_blob(path, blob)
        except (SyntaxError, UnicodeError):
            return False
        return test_texts(path, lines, spans, {name}).get(f"{path}::{name}", "") == text

    authors: dict[str, str] = {}
    for finding in tests.tests:
        if finding.test not in findings:
            continue
        pair = next(pair for pair in pairs if finding.test.startswith(pair.before + "::"))
        name = finding.test.removeprefix(pair.before + "::")
        path = pair.after
        commit = change.commit
        on_first_parent = True
        unit = ""
        while True:
            step = history[commit]
            candidates = ((parent, parent_path(parent, commit, path)) for parent in step.parents)
            parent, previous_path = next(((parent, previous_path) for parent, previous_path in candidates
                                          if has_test_text(parent, previous_path, name, finding.new)), ("", path))
            if parent not in history:
                break
            if on_first_parent and parent != step.parents[0]:
                on_first_parent = False
                unit = step.phase_author()
            commit = parent
            path = previous_path
        authors[finding.test] = step.phase_author() or unit or f"{commit} {step.subject}"
    return authors


class Approval(StrEnum):
    YES = "yes"
    NO = "no"


@dataclass(frozen=True)
class Clear:
    reply: Path


@dataclass(frozen=True)
class Waiting:
    question_id: str
    owner: str


@dataclass(frozen=True)
class InvalidAnswer:
    question_id: str
    owner: str
    reason: str


@dataclass(frozen=True)
class Answered:
    question_id: str
    owner: str
    approval: Approval
    answer: str
    time: datetime


class NoReview(StrEnum):
    MISSING = "missing"


type LandingRecord = Clear | Waiting | InvalidAnswer | Answered


def record_path(key: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}", key):
        raise BehaviorCheckError("Expected a behavior check key with 64 lowercase hex digits.")
    return state_root() / "behavior_checks" / f"{key}.json"


def read_record(key: str) -> LandingRecord | NoReview:
    path = record_path(key)
    try:
        stored = cast(object, json.loads(path.read_text(encoding="utf-8")))
        if not isinstance(stored, dict):
            raise ValueError("expected a JSON object")
        fields = cast(dict[str, object], stored)
        match fields:
            case {"state": "clear", "reply": str(reply)} if reply.strip():
                return Clear(Path(reply))
            case {"state": "waiting", "question_id": str(question), "owner": str(owner)} if question and owner:
                return Waiting(question, owner)
            case {"state": "invalid_answer", "question_id": str(question), "owner": str(owner), "reason": str(reason)} if question and owner and reason:
                return InvalidAnswer(question, owner, reason)
            case {"state": "yes" | "no" as answer, "question_id": str(question), "owner": str(owner), "answer": str(words), "time": str(time)} if question and owner and words.strip():
                return Answered(question, owner, Approval(answer), words, datetime.fromisoformat(time))
            case _:
                raise ValueError("invalid review state")
    except FileNotFoundError:
        return NoReview.MISSING
    except (ValueError, OSError) as error:
        raise BehaviorCheckError(f"{path}: {error}") from error


def write_record(key: str, record: LandingRecord) -> None:
    path = record_path(key)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if isinstance(record, Clear):
        state = {"state": "clear", "reply": str(record.reply)}
    elif isinstance(record, Waiting):
        state = {"state": "waiting", **asdict(record)}
    elif isinstance(record, InvalidAnswer):
        state = {"state": "invalid_answer", **asdict(record)}
    else:
        state = {"state": record.approval, "question_id": record.question_id, "owner": record.owner, "answer": record.answer,
                 "time": record.time.isoformat()}
    write_text_atomic(path, json.dumps(state, ensure_ascii=False) + "\n", new_file_mode=0o600)


def require_review(change: GitRange, *, timeout: float | None = None) -> None:
    tests = changed_tests(change, timeout=timeout)
    if tests is NoChangedTests.EMPTY:
        return
    record = read_record(tests.key)
    reason = "no review is on record"
    if isinstance(record, Clear) or isinstance(record, Answered) and record.approval is Approval.YES:
        verify = cast(Callable[[ChangedTests, Clear | Answered], bool],
                      import_module("scripts.production.behavior_review").verify_approval)
        if verify(tests, record):
            return
        reason = "the saved approval has no valid proof"
    elif isinstance(record, Waiting):
        reason = f"waiting for the answer to {record.question_id}"
    elif isinstance(record, InvalidAnswer):
        reason = record.reason
    elif isinstance(record, Answered):
        reason = f"{record.question_id} was answered No"
    names = ", ".join(test.test for test in tests.tests)
    raise BehaviorCheckError(f"Behavior check for {names}: {reason.rstrip('.')}.\nRun {change.command('run')}")


class BehaviorCommand(argparse.Namespace):
    key: str = ""
    production: str = ""
    action: str = ""
    repo: Path | None = None
    base: str = ""
    commit: str = ""
    question: Path | None = None


def add_range_arguments(parser: argparse.ArgumentParser, *, required: bool = True) -> None:
    _ = parser.add_argument("--repo", type=Path, required=required)
    _ = parser.add_argument("--base", required=required, default="")
    _ = parser.add_argument("--commit", required=required, default="")


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    add_range_arguments(commands.add_parser("find", help="List altered or removed tests."))
    add_range_arguments(commands.add_parser("check", help="Require proof before landing."))
    run = commands.add_parser("run", help="Start or collect the independent review.")
    add_range_arguments(run)
    _ = run.add_argument("--production", default="", help="The owner of a Review question.")
    _ = run.add_argument("--question", type=Path, help="Read the author's question from this JSON file.")
    status = commands.add_parser("status", help="Read the saved review or answer.")
    _ = status.add_argument("--key", default="")
    add_range_arguments(status, required=False)
    args = parser.parse_args(arguments, namespace=BehaviorCommand())
    if args.action == "status" and (args.key and (args.repo is not None or args.base or args.commit)
                                    or not args.key and not (args.repo is not None and args.base and args.commit)):
        status.error("pass either --key or --repo, --base and --commit")
    try:
        change = GitRange(args.repo or Path(), args.base, args.commit)
        if args.key:
            execute = cast(Callable[[str, GitRange, NoChangedTests, str, str, Path | NoQuestionDraft], int],
                           import_module("scripts.production.behavior_review").execute)
            return execute("status", change, NoChangedTests.EMPTY, args.key, "", NoQuestionDraft.OMITTED)
        change = change.pinned()
        if args.action == "check":
            require_review(change)
            return 0
        result = changed_tests(change)
        if result is NoChangedTests.EMPTY:
            return 0
        key = result.key
        if args.action == "find":
            print(json.dumps({"key": key, "tests": [asdict(test) for test in result.tests]}, indent=2))
            return 0
        execute = cast(Callable[[str, GitRange, ChangedTests, str, str, Path | NoQuestionDraft], int],
                       import_module("scripts.production.behavior_review").execute)
        return execute(args.action, change, result, key, args.production, args.question or NoQuestionDraft.OMITTED)
    except (BehaviorCheckError, OSError) as error:
        print(f"behavior_check: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    entry = cast(Callable[[list[str]], int], import_module("scripts.production.behavior_check").main)
    raise SystemExit(entry(sys.argv[1:]))
