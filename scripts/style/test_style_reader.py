"""Test the reader installation and the execution boundary with local commands."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shlex
import subprocess
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch

from scripts.style import style_config, style_reader


class ReaderClientTests(unittest.TestCase):
    def test_missing_and_stale_stamps_build_once_and_current_stamp_builds_none(self) -> None:
        for previous in ("missing", "stale", "checkout moves during build", "checkout moves during failed build"):
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
                verifier = root / "verify.sh"
                move = (f"git -C {shlex.quote(str(checkout))} checkout -q {second}\n"
                        if previous.startswith("checkout moves") else "")
                failure = (f"test $(wc -l < {shlex.quote(str(count))}) -eq 1 && exit 9\n"
                           if previous == "checkout moves during failed build" else "")
                _ = verifier.write_text(
                    "#!/bin/sh\n" + move + f"printf 'build\\n' >> {shlex.quote(str(count))}\n"
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

                with (patch.object(style_reader, "VERIFY_SCRIPT", verifier),
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
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "reader"
            for command, message in cases:
                with self.subTest(command=command):
                    _ = binary.write_text(f"#!/bin/sh\ncat >/dev/null\n{command}\n")
                    binary.chmod(0o755)
                    with self.assertRaisesRegex(style_reader.ReaderError, message):
                        _ = style_reader.ReaderClient(binary).read_names(root, ["blob"])

