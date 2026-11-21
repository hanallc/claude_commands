"""Test the reader installation and the execution boundary with local commands."""

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import os
import shlex
import subprocess
import sys
import tempfile
from threading import Barrier, Event, Lock
import unittest
from typing import cast, override
from unittest.mock import patch

from scripts.style import declarations, style_plan, style_reader, style_reader_oracle
from scripts.style.style_reader_test_support import oracle_reader


class ReaderClientTests(unittest.TestCase):
    def test_blob_facts_decode_existing_types_in_one_request_and_batch_module_queries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            repo.mkdir()
            def git(*arguments: str) -> str:
                return subprocess.run(["git", "-C", str(repo), *arguments],
                                      capture_output=True, text=True, check=True).stdout.strip()
            _ = git("init", "-q")
            _ = git("config", "user.name", "Reader test")
            _ = git("config", "user.email", "reader@example.invalid")
            sources = {"one.rs": "//! docs naïve 😀\nmod child;\nfn work() { work(); }\n",
                       "two.rs": "fn host() { let n = 1; }\n",
                       "crlf.rs": "fn work() {\r\n    work();\r\n}\r\n",
                       "separators.rs": "fn work() { // comment\rwork(); }\n",
                       "modules.rs": ('mod left { mod shared; } mod right { mod shared; }\n'
                                      'mod nested { #[path = "other.rs"] mod r#type; }\n'
                                      '#[cfg_attr(test, path = "unknown.rs")] mod conditional;\n'),
                       "broken.rs": "fn host() { /* unfinished"}
            for name, text in sources.items():
                _ = (repo / name).write_bytes(text.encode())
            _ = (repo / "bytes.rs").write_bytes(b"\xff")
            _ = git("add", ".")
            _ = git("commit", "-qm", "Create reader fixture")
            blobs = {name: git("rev-parse", f"HEAD:{name}") for name in (*sources, "bytes.rs")}
            count = root / "calls"
            binary = root / "oracle"
            _ = binary.write_text("#!/bin/sh\nprintf 'call\\n' >> " + shlex.quote(str(count))
                                  + "\nexec " + shlex.quote(sys.executable) + " -m scripts.style.style_reader_oracle\n")
            binary.chmod(0o755)
            client = style_reader.ReaderClient(binary)
            missing = "0" * 40
            with style_reader.use_client(client):
                facts = style_reader.read_blob_facts(repo, [missing], commits=["HEAD"])
            self.assertEqual(count.read_text(), "call\n")
            self.assertEqual(set(facts), {*blobs.values(), missing})
            for name, text in sources.items():
                with self.subTest(source=name):
                    reading = facts[blobs[name]]
                    self.assertIsInstance(reading, style_reader.BlobFacts)
                    if not isinstance(reading, style_reader.BlobFacts):
                        continue
                    file = reading.declared_file(text)
                    expected = declarations.read_declarations(text)
                    self.assertEqual(style_reader_oracle.declaration_value(file), style_reader_oracle.declaration_value(expected))
                    self.assertEqual(file.word_values, expected.word_values)
                    self.assertEqual(file.word_lines, expected.word_lines)
                    self.assertEqual(reading.name_sites(file), tuple(declarations.name_sites(body, item.declaration.name)
                        if isinstance(body := declarations.item_body(expected, item), declarations.ReadableBody)
                        else declarations.UnreadableBody() for item in expected.declared_items))
                    self.assertEqual(isinstance(reading.code, style_reader.UnreadableFact), name == "broken.rs")
                    self.assertEqual(style_plan.reader_module_declaration_text(reading), style_plan.module_declaration_text(text))
            self.assertEqual(facts[missing], style_reader.UnreadableFact(style_reader.UnreadableReason.MISSING_BLOB))
            replaced = facts[blobs["bytes.rs"]]
            self.assertIsInstance(replaced, style_reader.BlobFacts)
            if isinstance(replaced, style_reader.BlobFacts):
                self.assertEqual(style_reader_oracle.declaration_value(replaced.declared_file("�")),
                                 style_reader_oracle.declaration_value(declarations.read_declarations("�")))
                self.assertEqual(replaced.code, style_reader.UnreadableFact(style_reader.UnreadableReason.NOT_UTF8))
            one = facts[blobs["one.rs"]]
            self.assertIsInstance(one, style_reader.BlobFacts)
            if not isinstance(one, style_reader.BlobFacts):
                return
            self.assertEqual(one.module_scan(), style_reader.ModuleScanFact((style_reader.ModuleDeclarationFact(
                "child", (), style_reader.ModulePathForm.DEFAULT),)))
            query = style_reader.ModuleQuery(blobs["one.rs"], "src/lib.rs", (style_reader.ModuleMove(
                "src/child.rs", "src/renamed.rs", (), (), style_reader.NoCrateLibrary()),))
            missing_query = style_reader.ModuleQuery(missing, "src/lib.rs", query.moves)
            with style_reader.use_client(client):
                sites = style_reader.read_module_sites(repo, [query, missing_query])
            self.assertEqual(sites[(query.blob, query.path)], (True,))
            self.assertEqual(sites[(missing, query.path)], style_reader.UnreadableFact(style_reader.UnreadableReason.MISSING_BLOB))
            self.assertEqual(count.read_text(), "call\ncall\n")
            with self.subTest(binary="shared oracle"):
                configuration = root / "config"
                configuration.mkdir()
                _ = (configuration / "style_reader.conf").write_text(
                    "# whole: 1..\nprotocol=7\n# text:\nminimum_version=2.3.4\n")
                self.addCleanup(style_reader.reset_reader_cache)
                with patch.dict(os.environ, {"CONF_DIR": str(configuration)}), oracle_reader() as oracle:
                    self.assertEqual(style_reader.reader_binary(), oracle)
                    self.assertEqual(style_reader.reader_version_line(), "cargo-style 2.3.4 protocol 7")
                    self.assertEqual(style_reader.read_blob_facts(repo, [blobs["one.rs"]]), {blobs["one.rs"]: one})

    def test_reader_failure_and_invalid_output_raise_named_errors(self) -> None:
        cases = (
            ("printf 'reader detail\\n' >&2; exit 7", "reader exited 7: reader detail"),
            ("printf 'not json\\n'", "invalid JSON line"),
            ("printf '[]\\n'", "not an object"),
            ("printf '{}\\n'", "the reader"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "reader"
            for command, message in cases:
                with self.subTest(command=command):
                    _ = binary.write_text(f"#!/bin/sh\ncat >/dev/null\n{command}\n")
                    binary.chmod(0o755)
                    client = style_reader.ReaderClient(binary)
                    query = style_reader.ModuleQuery("blob", "src/lib.rs", ())
                    for operation in (lambda: client.read_names(root, ["blob"]),
                                      lambda: client.read_blob_facts(root, ["blob"]),
                                      lambda: client.read_module_sites(root, [query])):
                        with self.assertRaisesRegex(style_reader.ReaderError, message):
                            _ = operation()

class InstalledReaderTests(unittest.TestCase):
    root: Path = Path()
    calls: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.root / "bin").mkdir()
        self.calls = self.root / "calls"
        _ = (self.root / "style_reader.conf").write_text(
            "# one of: python, rust\nreader=rust\n# whole: 1..\nprotocol=7\n"
            + "# text:\nminimum_version=2.3.4\n")
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "CONF_DIR": str(self.root), "PATH": str(self.root / "bin")})))
        _ = os.environ.pop("STYLE_READER_BINARY", None)
        style_reader.reset_reader_cache()
        self.addCleanup(style_reader.reset_reader_cache)

    def installed(self, version: str, name: str = "cargo-style") -> Path:
        """Create a version command that records every argument."""
        binary = self.root / "bin" / name
        _ = binary.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> " + shlex.quote(str(self.calls))
            + "\nprintf '%s\\n' " + shlex.quote(version) + "\n")
        binary.chmod(0o755)
        return binary

    def test_installed_version_passes_at_the_minimum_and_above_it(self) -> None:
        with patch.object(subprocess, "Popen", wraps=subprocess.Popen) as processes:
            for version in ("2.3.4", "2.3.5", "2.4.0", "3.0.0", "2.10.0"):
                with self.subTest(version=version):
                    style_reader.reset_reader_cache()
                    binary = self.installed(f"cargo-style {version} protocol 7")
                    self.assertEqual(style_reader.reader_binary(), binary)
            self.assertEqual(processes.call_count, 5)
        self.assertEqual(self.calls.read_text().splitlines(), ["--version"] * 5)

    def test_install_failures_name_the_state_and_install_command_without_building(self) -> None:
        cases = (
            ("missing", "", "missing|not found|not installed"),
            ("older", "cargo-style 2.3.3 protocol 7", "older|too old|at least|minimum"),
            ("protocol", "cargo-style 2.3.4 protocol 8", "protocol"),
            ("unreadable", "not a cargo-style version", "unreadable|invalid|cannot parse|unrecognized"),
        )
        messages: list[str] = []
        with patch.object(subprocess, "Popen", wraps=subprocess.Popen) as processes:
            for state, version, pattern in cases:
                with self.subTest(state=state):
                    style_reader.reset_reader_cache()
                    if version:
                        _ = self.installed(version)
                    with self.assertRaises(style_reader.ReaderError) as caught:
                        _ = style_reader.reader_binary()
                    message = str(caught.exception)
                    self.assertRegex(message.lower(), pattern)
                    self.assertIn("scripts/install-cargo-style.sh", message)
                    with self.assertRaises(style_reader.ReaderError) as repeated:
                        _ = style_reader.reader_version_line()
                    self.assertEqual(str(repeated.exception), message)
                    messages.append(message)
            self.assertEqual(processes.call_count, 3)
        self.assertEqual(len(set(messages)), 4)
        self.assertEqual(self.calls.read_text().splitlines(), ["--version"] * 3)

    def test_explicit_binary_overrides_an_incompatible_path_binary(self) -> None:
        _ = self.installed("cargo-style 0.0.1 protocol 99")
        explicit = self.installed("cargo-style 2.3.4 protocol 7", "selected-reader")
        with patch.dict(os.environ, {"STYLE_READER_BINARY": str(explicit)}):
            self.assertEqual(style_reader.reader_binary(), explicit)
        self.assertEqual(self.calls.read_text().splitlines(), ["--version"])

    def test_version_is_checked_once_in_each_process(self) -> None:
        binary = self.installed("cargo-style 2.3.4 protocol 7")
        for _ in range(3):
            self.assertEqual(style_reader.reader_binary(), binary)
            self.assertEqual(style_reader.reader_version_line(), "cargo-style 2.3.4 protocol 7")
        self.assertEqual(self.calls.read_text().splitlines(), ["--version"])
        program = (
            "from scripts.style import style_reader\n"
            "for _ in range(3):\n"
            "    print(style_reader.reader_binary())\n")
        child = subprocess.run([sys.executable, "-c", program],
                               capture_output=True, text=True, check=True)
        self.assertEqual(child.stdout.splitlines(), [str(binary)] * 3)
        self.assertEqual(self.calls.read_text().splitlines(), ["--version", "--version"])
        for line in ("cargo-style 2.3.4 protocol 7", "invalid version"):
            with self.subTest(concurrent_version=line):
                style_reader.reset_reader_cache()
                _ = self.installed(line)
                begin = Barrier(8)
                arrivals_lock = Lock()
                all_arrived = Event()
                release_version = Event()
                arrivals = 0
                run_command = subprocess.run

                def read_once() -> Path | str:
                    nonlocal arrivals
                    _ = begin.wait(timeout=10)
                    with arrivals_lock:
                        arrivals += 1
                        if arrivals == 8:
                            all_arrived.set()
                    try:
                        return style_reader.reader_binary()
                    except style_reader.ReaderError as error:
                        return str(error)

                def version_command(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
                    del options
                    self.assertTrue(release_version.wait(timeout=10))
                    return run_command(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False)

                with (patch.object(subprocess, "run", side_effect=version_command) as processes,
                      ThreadPoolExecutor(max_workers=8) as executor):
                    futures = [executor.submit(read_once) for _ in range(8)]
                    try:
                        self.assertTrue(all_arrived.wait(timeout=10))
                    finally:
                        release_version.set()
                    observed = [future.result(timeout=10) for future in futures]
                self.assertEqual(processes.call_count, 1)
                self.assertEqual(len(set(observed)), 1)
                if line == "invalid version":
                    self.assertIn("unreadable version", str(observed[0]))
                else:
                    self.assertEqual(observed, [binary] * 8)
        self.assertEqual(self.calls.read_text().splitlines(), ["--version"] * 4)
