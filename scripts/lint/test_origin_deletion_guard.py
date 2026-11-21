"""Keep origin branch mutations in the leased deletion operation."""
from __future__ import annotations

import ast
import os
import re
import shlex
import subprocess
import tempfile
import textwrap
import unittest
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Literal
from unittest.mock import patch

from scripts.agents import remote
from scripts.lib import origin_deletion
from scripts.lib.git_test_support import commit_repository, fixture_git_environment
from scripts.production import production_doc, staff
from scripts.production.staff_test_support import fixture_member


ROOT = Path(__file__).resolve().parents[2]
OPERATION = Path("scripts/lib/origin_deletion.py")
SHELL_COMMAND = re.compile(r"(?<![\w.-])(?:git|gh)(?=\s)")
SHELL_LEXEMES = re.compile(r"""\\.|[$][(]|['"#()\x60]|""" + SHELL_COMMAND.pattern)
FENCE = re.compile(r"^[ \t]*(?P<marker>\x60{3,}|~{3,})[ \t]*(?P<language>[^\s\x60~]*).*")
INLINE_DELIMITER = re.compile(r"(?<![\\\x60])\x60+(?!\x60)")
SHELL_LANGUAGES = {"", "bash", "console", "fish", "sh", "shell", "shell-session", "zsh"}
UNKNOWN_WORD = "?"


def word(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(map(word, node.values))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return word(node.left) + word(node.right)
    return UNKNOWN_WORD


def changes_origin_branch(words: Sequence[str]) -> bool:
    if "merge" in words and "--delete-branch" in words:
        return True
    if "push" not in words:
        return False
    arguments = words[words.index("push") + 1:]
    operands = [item for item in arguments if not item.startswith("-")]
    if not operands:
        return "--delete" in arguments
    refs = operands[1:]
    if "--delete" in arguments or "-d" in arguments:
        return not refs or any(not item.startswith("refs/") or item.startswith("refs/heads/") for item in refs)
    forced = any(arg in ("--force", "-f") or arg.startswith("--force-with-lease") for arg in arguments)
    for item in refs:
        source, colon, destination = item.partition(":")
        if colon and destination and (not source or source == "+"):
            if not destination.startswith("refs/") or destination.startswith("refs/heads/"):
                return True
        if (source.startswith("+") or forced) and (not destination.startswith("refs/") or destination.startswith("refs/heads/")):
            return True
    return False


def shell_command_offsets(source: str) -> Iterator[int]:
    quote = ""
    substitutions: list[tuple[str, str]] = []
    comment_end = 0
    for match in SHELL_LEXEMES.finditer(source):
        offset = match.start()
        token = match.group()
        if offset < comment_end or token.startswith("\\") and quote != "'":
            continue
        if quote == "'":
            if token.endswith(quote):
                quote = ""
        elif token == "$(":
            substitutions.append((")", quote))
            quote = ""
        elif token == "\x60":
            if not quote and substitutions and substitutions[-1][0] == token:
                _, quote = substitutions.pop()
            else:
                substitutions.append((token, quote))
                quote = ""
        elif quote:
            if token == quote:
                quote = ""
        elif token in ("'", '"'):
            quote = token
        elif token == "#" and (offset == 0 or source[offset - 1] in " \t\r\n;&|()"):
            comment_end = source.find("\n", offset)
            if comment_end < 0:
                return
        elif substitutions and token == "(":
            substitutions.append((")", ""))
        elif substitutions and token == substitutions[-1][0]:
            _, quote = substitutions.pop()
        elif token in ("git", "gh"):
            yield offset


def shell_mutations(source: str) -> Iterator[tuple[int, list[str]]]:
    joined = source.replace("\\\n", "  ")
    for offset in shell_command_offsets(joined):
        rest = joined[offset:].split("\n", 1)[0]
        lexer = shlex.shlex(rest, posix=True, punctuation_chars=";&|()<>")
        lexer.whitespace_split = True
        words: list[str] = []
        try:
            for token in lexer:
                if token and all(character in ";&|()<>" for character in token):
                    break
                words.append(token)
        except ValueError:
            # A command substitution can end inside a quoted assignment.
            pass
        if changes_origin_branch(words):
            yield source.count("\n", 0, offset) + 1, words


def python_mutations(source: str) -> Iterator[tuple[int, list[str]]]:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            items = node.args
        elif isinstance(node, (ast.List, ast.Tuple)):
            items = node.elts
        else:
            continue
        words = list(map(word, items))
        if changes_origin_branch(words):
            yield node.lineno, words
        if words and Path(words[0]).name in SHELL_LANGUAGES and "-c" in words:
            script = words[words.index("-c") + 1:]
        elif isinstance(node, ast.Call) and any(
            arg.arg == "shell" and isinstance(arg.value, ast.Constant) and arg.value.value is True
            for arg in node.keywords
        ):
            script = words[:1]
        else:
            continue
        if script:
            for _line, shell_words in shell_mutations(script[0]):
                yield node.lineno, shell_words


def inline_shell_nodes(source: str, first_line: int) -> Iterator[tuple[int, str]]:
    delimiters = list(INLINE_DELIMITER.finditer(source))
    index = 0
    while index < len(delimiters):
        opening = delimiters[index]
        index += 1
        for closing_index in range(index, len(delimiters)):
            closing = delimiters[closing_index]
            if closing.group() == opening.group():
                yield first_line + source.count("\n", 0, opening.end()), source[opening.end():closing.start()]
                index = closing_index + 1
                break


def markdown_code_nodes(source: str) -> Iterator[tuple[int, Literal["shell", "python"], str]]:
    lines = source.splitlines(keepends=True)
    index = 0
    prose_start = 0
    while index < len(lines):
        opening = FENCE.match(lines[index])
        if opening is None:
            index += 1
            continue
        for first_line, inline in inline_shell_nodes("".join(lines[prose_start:index]), prose_start + 1):
            yield first_line, "shell", inline
        marker = opening["marker"]
        close = re.compile(r"^[ \t]*" + re.escape(marker[0]) + r"{" + str(len(marker)) + r",}[ \t]*$")
        start = index + 1
        index = start
        while index < len(lines) and not close.match(lines[index]):
            index += 1
        body = textwrap.dedent("".join(lines[start:index]))
        language = opening["language"].casefold()
        if language in SHELL_LANGUAGES:
            yield start + 1, "shell", body
        elif language in ("py", "python", "python3"):
            yield start + 1, "python", body
        index += 1
        prose_start = index
    for first_line, inline in inline_shell_nodes("".join(lines[prose_start:]), prose_start + 1):
        yield first_line, "shell", inline


def markdown_mutations(source: str) -> Iterator[tuple[int, list[str]]]:
    for first_line, language, code in markdown_code_nodes(source):
        mutations = python_mutations(code) if language == "python" else shell_mutations(code)
        for line, words in mutations:
            yield first_line + line - 1, words


class OriginDeletionGuardTests(unittest.TestCase):
    def test_origin_mutations_use_the_operation(self) -> None:
        faults: list[str] = []
        detectors = {".py": python_mutations, ".sh": shell_mutations, ".md": markdown_mutations}
        for folder in (ROOT / "scripts", ROOT / "commands", ROOT / ".github"):
            for path in folder.rglob("*"):
                relative = path.relative_to(ROOT)
                if (path.suffix not in detectors or path.name.startswith("test_")
                        or relative == OPERATION or not path.is_file()):
                    continue
                source = path.read_text(encoding="utf-8")
                for line, words in detectors[path.suffix](source):
                    # This caller lists only refs/simplify/claim/* before deleting a claim.
                    if relative == Path("scripts/direct/simplify_daily.py") and words[-1:] in ([":?"], ["?:?"]):
                        continue
                    faults.append(f"{relative}:{line}: {' '.join(words)}")
        self.assertEqual(faults, [])

    def test_guard_recognizes_git_and_gh_command_forms(self) -> None:
        for source, form, expected in (
            ('git push origin --delete topic', "shell", (1,)),
            ('/usr/bin/git -C /tmp/checkout push origin :refs/heads/topic', "shell", (1,)),
            ('git push origin --delete refs/simplify/claim/day', "shell", ()),
            ('git push origin :refs/heads/topic', "shell", (1,)),
            ('git push origin :refs/simplify/claim/day', "shell", ()),
            ('git push origin +HEAD:refs/heads/topic', "shell", (1,)),
            ('git push origin HEAD:refs/heads/topic', "shell", ()),
            ('OUT="$(gh pr merge 12 --delete-branch 2>&1)"', "shell", (1,)),
            ('git push origin :', "shell", ()),
            ('subprocess.run(["git", "push", "origin", "--delete", branch])', "python", (1,)),
            ('git(path, "push", "origin", f":refs/heads/{branch}")', "python", (1,)),
            ('git(path, "push", "origin", f"+{tip}:refs/heads/{branch}")', "python", (1,)),
            ('git(path, "push", "origin", "--force", f"{tip}:refs/heads/{branch}")', "python", (1,)),
            ('git(path, "push", "origin", "+HEAD:refs/simplify/last")', "python", ()),
            ('subprocess.run(["gh", "pr", "merge", "--delete-branch"])', "python", (1,)),
            ('# git push origin --delete topic', "shell", ()),
            ('printf "%s" "git push origin --delete topic"', "shell", ()),
            ('print("git push origin --delete topic")', "python", ()),
            ('Run `git push origin --delete [branch-name]`.', "markdown", (1,)),
            ('Delete with\n`git push origin --delete direct/<plan-slug>`.', "markdown", (2,)),
            ('```bash\n# Delete the release branch\ngit push origin :release-$VERSION\n```', "markdown", (3,)),
            ('```bash\n# Comment\ngit push origin \\\n --delete refs/heads/topic\n```', "markdown", (3,)),
            ('```python\nsubprocess.run(["git", "push", "origin", "--delete", branch])\n```', "markdown", (2,)),
            ('Use `~/.claude/scripts/lib/py ~/.claude/scripts/lib/origin_deletion.py "$PWD" origin ' +
             'refs/heads/topic "$EXPECTED_COMMIT"`.', "markdown", ()),
            ('Do not run git push origin --delete topic.', "markdown", ()),
            ('```text\nRun `git push origin --delete topic`.\n```', "markdown", ()),
            ('```sh\n# git push origin --delete topic\necho "git push origin --delete topic"\n```', "markdown", ()),
        ):
            with self.subTest(source=source):
                detectors = {"shell": shell_mutations, "python": python_mutations, "markdown": markdown_mutations}
                self.assertEqual(tuple(line for line, _ in detectors[form](source)), expected)

    def test_deletion_checks_the_lease_and_current_staff_authority(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repository, destination = root / "repo", root / "origin.git"
            record = root / "staff.json"
            environment = {**fixture_git_environment(root), "STAFF_FILE": str(record), "GIT_ALLOW_PROTOCOL": "file"}
            with patch.dict(os.environ, environment, clear=True):
                def git(path: Path, *args: str) -> str:
                    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()

                _ = git(root, "init", "-qb", "main", str(repository))
                _ = git(root, "init", "--bare", "-qb", "main", str(destination))
                base = commit_repository(repository, "Base", allow_empty=True)
                later = commit_repository(repository, "Later", allow_empty=True)
                _ = git(repository, "remote", "add", "origin", str(destination))
                for scenario, expected_result in (
                    ("deleted", origin_deletion.Deleted),
                    ("absent", origin_deletion.Absent),
                    ("lease mismatch", origin_deletion.Kept),
                    ("registered after preview", origin_deletion.Kept),
                    ("unresolved registration", origin_deletion.Kept),
                    ("detached registration", origin_deletion.Kept),
                    ("remote registration without checkout", origin_deletion.Kept),
                ):
                    with self.subTest(scenario=scenario):
                        _ = record.write_text('{"schema": 2, "rows": []}')
                        ref = f"refs/heads/{scenario.replace(' ', '-')}"
                        if scenario != "absent":
                            sha = later if scenario == "lease mismatch" else base
                            _ = git(repository, "push", "origin", f"{sha}:{ref}")
                        self.assertEqual(production_doc.origin_ref_protection(ref), "")
                        document = root / "production.md"
                        staff_id = ""
                        machine = remote.local_machine()
                        if scenario.startswith(("registered", "unresolved", "detached", "remote")):
                            if scenario == "detached registration":
                                _ = git(repository, "switch", "--detach", later)
                                text = f"- **Showrunner checkout:** `{repository}`\n"
                            else:
                                text = f"- **Merge branch:** {ref.removeprefix('refs/heads/')}\n" if scenario.startswith("registered") else ""
                            _ = document.write_text(text)
                            if scenario.startswith("remote"):
                                machine = "fixture-remote"
                            row = fixture_member("showrunner", name="fixture", doc=staff.LiveDoc(machine, document))
                            staff_id = str(row.id)
                        before = git(destination, "for-each-ref", "--format=%(objectname)", ref)
                        if scenario.startswith("remote"):
                            def machine_alias(name: str) -> remote.MachineAlias:
                                return remote.MachineAlias(name)

                            with (patch.object(remote, "machine_alias", machine_alias),
                                  patch.object(production_doc, "_remote_protection_command",
                                               return_value=subprocess.CompletedProcess(["fixture"], 0, "", ""))):
                                result = origin_deletion.delete(repository, "origin", ref, base)
                        else:
                            result = origin_deletion.delete(repository, "origin", ref, base)
                        self.assertIsInstance(result, expected_result)
                        after = git(destination, "for-each-ref", "--format=%(objectname)", ref)
                        if isinstance(result, origin_deletion.Kept):
                            self.assertEqual(after, before)
                            if scenario.startswith(("unresolved", "detached", "remote")):
                                self.assertIn(staff_id, result.reason)
                                self.assertIn(str(document), result.reason)
                                self.assertIn(machine, result.reason)
                                self.assertIn("no checkout with an attached HEAD", result.reason)
                        else:
                            self.assertEqual(after, "")
