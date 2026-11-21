"""The post-push hook publishes the AWS page after a main push that changes its code."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
from typing import override
import unittest

HOOK = Path(__file__).resolve().parents[2] / ".claude/config/post_push.sh"
PUBLISHER = """#!/bin/sh
printf '%s\\n' "$*" >> "$PUBLISH_LOG"
[ ! -f "$PUBLISH_FAIL" ]
"""


class AwsPagePostPushTests(unittest.TestCase):
    repo: Path = Path()
    publish_log: Path = Path()
    publish_fail: Path = Path()

    @override
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = root / "repo"
        self.publish_log = root / "publishes"
        self.publish_fail = root / "publish-fails"
        self.repo.mkdir()
        _ = self.git("init", "-q", "-b", "main")
        self.write("scripts/lib/py", PUBLISHER).chmod(0o755)
        _ = self.write(".claude/config/post_push.sh", HOOK.read_text())
        _ = self.write("scripts/aws_target/aws_page.py", "page\n")
        _ = self.write("docs/notes.md", "notes\n")
        _ = self.commit("seed")

    def git(self, *arguments: str) -> str:
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *arguments],
                              cwd=self.repo, check=True, capture_output=True, text=True).stdout.strip()

    def write(self, relative: str, text: str) -> Path:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(text)
        return path

    def commit(self, message: str) -> str:
        _ = self.git("add", "-A")
        _ = self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def run_hook(self, sha: str) -> subprocess.CompletedProcess[str]:
        environment = {**os.environ, "LANDED_SHA": sha, "LANDED_BRANCH": "main",
                       "PUBLISH_LOG": str(self.publish_log), "PUBLISH_FAIL": str(self.publish_fail)}
        return subprocess.run(["bash", ".claude/config/post_push.sh"], cwd=self.repo, env=environment,
                              check=False, capture_output=True, text=True)

    def publishes(self) -> list[str]:
        return self.publish_log.read_text().splitlines() if self.publish_log.exists() else []

    def record(self) -> str:
        path = Path(self.git("rev-parse", "--path-format=absolute", "--git-common-dir")) / "aws-page-published"
        return path.read_text().strip() if path.exists() else ""

    def test_a_push_that_changes_the_page_code_publishes_the_page_once(self) -> None:
        first = self.git("rev-parse", "HEAD")
        result = self.run_hook(first)
        self.assertEqual((result.returncode, self.publishes(), self.record()),
                         (0, ["scripts/aws_target/aws_page.py publish"], first))
        _ = self.write("docs/notes.md", "more notes\n")
        unrelated = self.commit("notes")
        self.assertEqual((self.run_hook(unrelated).returncode, len(self.publishes()), self.record()), (0, 1, unrelated))
        _ = self.write("scripts/aws_target/aws_page.py", "new page\n")
        changed = self.commit("page")
        self.assertEqual((self.run_hook(changed).returncode, len(self.publishes()), self.record()), (0, 2, changed))

    def test_a_checkout_at_another_commit_publishes_nothing(self) -> None:
        first = self.git("rev-parse", "HEAD")
        _ = self.write("scripts/aws_target/aws_page.py", "new page\n")
        _ = self.commit("page")
        result = self.run_hook(first)
        self.assertEqual((result.returncode, self.publishes(), self.record()), (0, [], ""))
        self.assertIn("publishes on the next promotion or the hourly run", result.stdout)

    def test_a_failed_publish_keeps_the_push_and_tries_again_on_the_next_push(self) -> None:
        first = self.git("rev-parse", "HEAD")
        _ = self.publish_fail.write_text("")
        result = self.run_hook(first)
        self.assertEqual((result.returncode, len(self.publishes()), self.record()), (0, 1, ""))
        self.assertIn("publish failed", result.stdout)
        self.publish_fail.unlink()
        _ = self.write("docs/notes.md", "more notes\n")
        later = self.commit("notes")
        self.assertEqual((self.run_hook(later).returncode, len(self.publishes()), self.record()), (0, 2, later))


if __name__ == "__main__":
    _ = unittest.main()
