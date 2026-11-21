"""Keep origin branch mutations in the leased deletion operation."""
from __future__ import annotations

import ast
import os
import re
import shlex
import subprocess
import tempfile
import unittest
from collections.abc import Iterator, Sequence
from pathlib import Path
from unittest.mock import patch

from scripts.agents import remote
from scripts.lib import origin_deletion
from scripts.lib.git_test_support import commit_repository, fixture_git_environment
from scripts.production import production_doc, staff
from scripts.production.staff_test_support import fixture_member


ROOT = Path(__file__).resolve().parents[2]
OPERATION = Path("scripts/lib/origin_deletion.py")
SHELL_COMMAND = re.compile(r"(?<![\w.-])(?:git|gh)(?=\s)")
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


def shell_mutations(source: str) -> Iterator[tuple[int, list[str]]]:
    source = source.replace("\\\n", " ")
    for match in SHELL_COMMAND.finditer(source):
        rest = source[match.start():].split("\n", 1)[0]
        lexer = shlex.shlex(rest, posix=True, punctuation_chars=";&|()<>")
        lexer.whitespace_split = True
        words: list[str] = []
        try:
            for token in lexer:
                if token and all(character in ";&|()<>" for character in token):
                    break
                words.append(token)
        except ValueError:
            # A command substitution can end inside a double-quoted assignment.
            pass
        if changes_origin_branch(words):
            yield source.count("\n", 0, match.start()) + 1, words


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
        for value in words:
            for _line, shell_words in shell_mutations(value):
                yield node.lineno, shell_words


class OriginDeletionGuardTests(unittest.TestCase):
    def test_origin_mutations_use_the_operation(self) -> None:
        faults: list[str] = []
        for folder in (ROOT / "scripts", ROOT / "commands", ROOT / ".github"):
            for path in folder.rglob("*"):
                relative = path.relative_to(ROOT)
                if (path.suffix not in (".py", ".sh") or path.name.startswith("test_")
                        or relative == OPERATION or not path.is_file()):
                    continue
                source = path.read_text(encoding="utf-8")
                mutations = python_mutations(source) if path.suffix == ".py" else shell_mutations(source)
                for line, words in mutations:
                    # This caller lists only refs/simplify/claim/* before deleting a claim.
                    if relative == Path("scripts/direct/simplify_daily.py") and words[-1:] in ([":?"], ["?:?"]):
                        continue
                    faults.append(f"{relative}:{line}: {' '.join(words)}")
        self.assertEqual(faults, [])

    def test_guard_recognizes_git_and_gh_command_forms(self) -> None:
        for source, python, forbidden in (
            ('git push origin --delete topic', False, True),
            ('/usr/bin/git -C /tmp/checkout push origin :refs/heads/topic', False, True),
            ('git push origin --delete refs/simplify/claim/day', False, False),
            ('git push origin :refs/heads/topic', False, True),
            ('git push origin :refs/simplify/claim/day', False, False),
            ('git push origin +HEAD:refs/heads/topic', False, True),
            ('git push origin HEAD:refs/heads/topic', False, False),
            ('OUT="$(gh pr merge 12 --delete-branch 2>&1)"', False, True),
            ('git push origin :', False, False),
            ('subprocess.run(["git", "push", "origin", "--delete", branch])', True, True),
            ('git(path, "push", "origin", f":refs/heads/{branch}")', True, True),
            ('git(path, "push", "origin", f"+{tip}:refs/heads/{branch}")', True, True),
            ('git(path, "push", "origin", "--force", f"{tip}:refs/heads/{branch}")', True, True),
            ('git(path, "push", "origin", "+HEAD:refs/simplify/last")', True, False),
            ('subprocess.run(["gh", "pr", "merge", "--delete-branch"])', True, True),
        ):
            with self.subTest(source=source):
                matches = python_mutations(source) if python else shell_mutations(source)
                self.assertEqual(bool(list(matches)), forbidden)

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
