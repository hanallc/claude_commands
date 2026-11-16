"""Test the reader installation and the execution boundary with local commands."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import shlex
import subprocess
import sys
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch

from scripts.style import declarations, style_config, style_reader, style_reader_oracle


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

    def test_missing_and_stale_stamps_build_once_and_current_stamp_builds_none(self) -> None:
        for previous in ("missing", "stale", "checkout moves during build", "checkout moves during failed build",
                         "private configuration"):
            with self.subTest(previous=previous), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                install = root / "installed"
                checkout = root / "checkout"
                checkout.mkdir()

                def git(*arguments: str) -> str:
                    return subprocess.run(["git", "-C", str(checkout), *arguments],
                                          capture_output=True, check=True, text=True).stdout.strip()

                _ = git("init", "-q")
                _ = git("config", "user.name", "Reader Test")
                _ = git("config", "user.email", "reader@example.invalid")
                project = checkout / "rust" / "style_reader"
                project.mkdir(parents=True)
                _ = (project / "main.rs").write_text("first source")
                _ = git("add", ".")
                _ = git("commit", "-qm", "Create the first reader source")
                first = git("rev-parse", "HEAD")
                _ = (project / "main.rs").write_text("second source")
                _ = git("add", ".")
                _ = git("commit", "-qm", "Change the reader source")
                second = git("rev-parse", "HEAD")
                expected_tree = git("rev-parse", "HEAD:rust/style_reader")
                if previous.startswith("checkout moves"):
                    _ = git("checkout", "-q", first)
                source = root / "built-reader"
                _ = source.write_text("#!/bin/sh\nprintf 'ready\\n'\n")
                source.chmod(0o755)
                count = root / "builds"
                arguments = root / "arguments"
                observed_environment = root / "environment"
                verifier = root / "verify.sh"
                move = (f"git -C {shlex.quote(str(checkout))} checkout -q {second}\n"
                        if previous.startswith("checkout moves") else "")
                failure = (f"test $(wc -l < {shlex.quote(str(count))}) -eq 1 && exit 9\n"
                           if previous == "checkout moves during failed build" else "")
                _ = verifier.write_text(
                    "#!/bin/sh\n" + move + f"printf 'build\\n' >> {shlex.quote(str(count))}\n"
                    + f'printf "%s\\n%s\\n" "${{CONF_DIR+set}}" "${{STYLE_READER_TEST_VALUE}}" > {shlex.quote(str(observed_environment))}\n'
                    + failure + f"printf '%s\\n' \"$@\" > {shlex.quote(str(arguments))}\n"
                    + f"printf 'binary: %s\\n' {shlex.quote(str(source))}\n"
                )
                if previous == "stale":
                    old = install / "old"
                    old.mkdir(parents=True)
                    _ = (old / "stamp").write_text("an old tree\n")
                    (install / "current").symlink_to("old", target_is_directory=True)
                barrier = Barrier(2)

                def resolve() -> Path:
                    _ = barrier.wait()
                    return style_reader.reader_binary()

                environment = ({"CONF_DIR": str(root / "config"), "STYLE_READER_TEST_VALUE": str(root)}
                               if previous == "private configuration" else {})
                with (patch.dict(os.environ, environment),
                      patch.object(style_reader, "VERIFY_SCRIPT", verifier),
                      patch.object(style_reader, "CHECKOUT", checkout),
                      patch.object(style_config, "reader_install_folder", return_value=install)):
                    with ThreadPoolExecutor(max_workers=2) as threads:
                        futures = (threads.submit(resolve), threads.submit(resolve))
                        results = [future.result() for future in futures]
                    self.assertEqual(results[0], results[1])
                    self.assertEqual(results[0].read_bytes(), source.read_bytes())
                    self.assertEqual(count.read_text(), "build\n" * (2 if previous.startswith("checkout moves") else 1))
                    self.assertEqual(arguments.read_text().splitlines()[:3], ["build", "style_reader", "--release"])
                    self.assertEqual(style_reader.reader_binary(), results[0])
                    self.assertEqual(count.read_text(), "build\n" * (2 if previous.startswith("checkout moves") else 1))
                    self.assertEqual((results[0].parent / "stamp").read_text().strip(), results[0].parent.name.split("-")[0])
                    self.assertEqual((results[0].parent / "stamp").read_text().strip(), expected_tree)
                    if previous == "private configuration":
                        self.assertEqual(observed_environment.read_text().splitlines(), ["", str(root)])
                        self.assertEqual(os.environ["CONF_DIR"], environment["CONF_DIR"])

    def test_failed_build_keeps_the_previous_installation_and_reports_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install = root / "installed"
            old = install / "old"
            old.mkdir(parents=True)
            _ = (old / "stamp").write_text("old tree\n")
            _ = (old / "style_reader").write_text("old binary")
            (install / "current").symlink_to("old", target_is_directory=True)
            verifier = root / "verify.sh"
            _ = verifier.write_text("#!/bin/sh\nprintf 'build failed\\n'\nprintf 'compiler detail\\n' >&2\nexit 9\n")
            with (patch.object(style_reader, "VERIFY_SCRIPT", verifier),
                  patch.object(style_config, "reader_install_folder", return_value=install),
                  self.assertRaisesRegex(style_reader.ReaderError, "build failed[\\s\\S]*compiler detail")):
                _ = style_reader.reader_binary()
            self.assertEqual((install / "current" / "style_reader").read_text(), "old binary")
            self.assertEqual((install / "current").resolve(), old)

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

