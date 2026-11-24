from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.prioritize import strip_generated as strip

SCRIPT = Path(strip.__file__)
RANKED = b"---\ntitle: one\nbacklog_score: 7\nbacklog_rank: 2\n---\nbody backlog_rank: 9\n"
CLEAN = b"---\ntitle: one\n---\nbody backlog_rank: 9\n"


def packet(data: bytes) -> bytes:
    return f"{len(data) + 4:04x}".encode() + data


def git_request(*lines: str) -> bytes:
    return b"".join(packet(f"{line}\n".encode()) for line in lines) + strip.FLUSH


class StripGeneratedTests(unittest.TestCase):
    def test_per_file_mode_strips_the_generated_fields(self) -> None:
        done = subprocess.run([sys.executable, str(SCRIPT)], input=RANKED, capture_output=True, check=True, timeout=30)
        self.assertEqual(done.stdout, CLEAN)

    def test_process_mode_cleans_each_file_of_one_git_add(self) -> None:
        large = b"---\nbacklog_rank: 1\n---\n" + b"x" * (3 * strip.PACKET_DATA_LIMIT + 5)
        files = {"issues/ranked.md": RANKED, "issues/large.md": large, "issues/empty.md": b"",
                 "issues/plain.md": b"no frontmatter\r\nbacklog_rank: 3\n"}
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)

            def git(*args: str, input_data: bytes | None = None) -> bytes:
                return subprocess.run(["git", "-C", str(repository), *args], input=input_data, capture_output=True,
                                      check=True, timeout=30).stdout

            _ = git("init", "-q")
            _ = (repository / ".gitattributes").write_text("issues/*.md filter=strip\n")
            (repository / "issues").mkdir()
            for name, data in files.items():
                _ = (repository / name).write_bytes(data)
            _ = git("-c", f"filter.strip.process={sys.executable} {SCRIPT} {strip.PROCESS_FLAG}",
                    "-c", "filter.strip.required=true", "add", "-A")
            batch = git("cat-file", "--batch", input_data="".join(f":{name}\n" for name in files).encode())
        staged: dict[str, bytes] = {}
        for name in files:
            header, batch = batch.split(b"\n", 1)
            size = int(header.split()[2])
            staged[name], batch = batch[:size], batch[size + 1:]
        self.assertEqual(staged, {**files, "issues/ranked.md": CLEAN,
                                  "issues/large.md": b"---\n---\n" + b"x" * (3 * strip.PACKET_DATA_LIMIT + 5)})

    def test_process_mode_reports_an_error_for_a_file_that_is_not_utf8(self) -> None:
        source = io.BytesIO(git_request("git-filter-client", "version=2")
                            + git_request("capability=clean", "capability=smudge")
                            + git_request("command=clean", "pathname=issues/bad.md")
                            + packet(b"---\n\xff") + strip.FLUSH)
        sink = io.BytesIO()
        strip.serve(source, sink)
        self.assertEqual(sink.getvalue(),
                         git_request("git-filter-server", "version=2") + git_request("capability=clean")
                         + git_request("status=error"))


if __name__ == "__main__":
    _ = unittest.main()
