"""Exercise the console check with a real server and browser."""
from __future__ import annotations

from base64 import urlsafe_b64decode
from collections.abc import Callable, Generator, Mapping, Sequence
import compileall
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager, redirect_stdout, suppress
from dataclasses import replace
from html import unescape
from http.client import HTTPConnection
from io import StringIO
from pathlib import Path
from typing import BinaryIO, cast, override
import json
import os
import py_compile
import re
import signal
import socket
import sqlite3
import shutil
import subprocess
import sys
import time
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from scripts.disks import disks_page, measure
from scripts.lib import conf, conf_reads
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import config_environment
from scripts.production import change_routing, console_browser, console_check, console_server, console_settings
from scripts.production.console_browser import Browser, FaultKind, PageFault, PageResult, Pipe, Viewport, VisitCompletion, managed_process, message_object
from scripts.review_links import accounts_page, change_browser, change_page, console, review_page, serve
from scripts.production.test_console_fixtures import failed_status, first_page

REPOSITORY = Path(__file__).resolve().parents[2]


def browser_available() -> bool:
    try:
        _ = console_check.browser_command()
    except ValueError:
        return False
    return True


type BrowserVisit = Callable[[Browser, str, Viewport, float, str, str], PageResult]


class ConsoleCheckTests(unittest.TestCase):
    candidate: Path = Path()
    store: Path = Path()

    scripts: Path = Path()

    @classmethod
    @override
    def setUpClass(cls) -> None:
        cls.scripts = Path(cls.enterClassContext(tempfile.TemporaryDirectory(prefix="console-scripts-"))) / "scripts"
        ignored = shutil.ignore_patterns("__pycache__", "console_cases", "test_*.py")

        def skip_tests(directory: str, names: list[str]) -> set[str]:
            runtime: set[str] = {"test_hang_guard.py", "test_phase.py"} if Path(directory).name == "lib" else set()
            return ignored(directory, names) - runtime

        _ = shutil.copytree(REPOSITORY / "scripts", cls.scripts, ignore=skip_tests)
        fixture = Path("production/test_console_fixtures.py")
        _ = shutil.copyfile(REPOSITORY / "scripts" / fixture, cls.scripts / fixture)
        accounts = cls.scripts / "accounts/accounts.py"
        source = accounts.read_text()
        start = source.index("def _local_codex_processes()")
        end = source.index("def _session_config_dir(", start)
        # This fixture has no native Codex sessions. Keep the real Accounts child and its other readers.
        _ = accounts.write_text(source[:start] + "def _local_codex_processes():\n    return {}\n\n\n" + source[end:])
        if not compileall.compile_dir(cls.scripts, quiet=2, invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH):
            raise ValueError("Cannot compile the console script fixture.")

    @override
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="console-fixture-")))
        self.candidate = root / "candidate"
        config = self.candidate / "config"
        config.mkdir(parents=True)
        for name in ("merge_gate", "review_links", "statusline"):
            _ = shutil.copyfile(REPOSITORY / f"config/{name}.conf", config / f"{name}.conf")
        _ = shutil.copytree(self.scripts, self.candidate / "scripts", copy_function=os.link)
        module = "scripts.production.test_console_fixtures"
        _ = (config / "console.conf").write_text(
            f"page.first.label=First\npage.first.title=First\npage.first.icon=inbox\n"
            + f"page.first.url=python:{module}:first_page\n"
            + "page.second.label=Second\npage.second.title=Second\npage.second.icon=inbox\n"
            + f"page.second.url=python:{module}:second_page\n"
            + f"rebuilds.url=python:{module}:rebuilds_page\n")
        self.store = root / "store"
        (self.store / "fixture").mkdir(parents=True)
        for name in ("first", "second", "rebuilds"):
            self.write_page(name, "<h1>Page ready</h1>")
        staff_file = root / "staff.json"
        _ = staff_file.write_text('{"schema":2,"rows":[]}')
        sessions = root / "sessions"
        sessions.mkdir()
        self.enterContext(patch.dict(os.environ, {
            "SESSION_TEMP_ROOT": str(sessions), "SESSION_FORMER_ROOT": str(root / "former-sessions"),
            "HOME": str(root / "home"), "CONF_DIR": str(config), "REVIEW_LINKS_ROOT": str(self.store), "STAFF_FILE": str(staff_file),
            "REVIEW_LINKS_HOST": "127.0.0.1",
            "REBUILD_NEEDS_DIR": str(root / "needs"), "FOR_YOU_ACTIONS_DIR": str(root / "for-you"),
            "HERDR_PANE_RECORD_DIR": str(root / "pane-records"),
        }))

    def copy_candidate(self, base: Path) -> None:
        _ = shutil.copytree(self.candidate, base, symlinks=True, ignore=shutil.ignore_patterns("scripts"))
        _ = shutil.copytree(self.candidate / "scripts", base / "scripts", symlinks=True, copy_function=os.link)

    def replace_script(self, path: Path, contents: str) -> None:
        # The fixture shares source files until a test replaces one.
        path.unlink()
        _ = path.write_text(contents)

    def write_page(self, name: str, content: str) -> None:
        _ = (self.store / f"fixture/{name}.html").write_text(
            "<!doctype html><meta charset=utf-8><title>Fixture</title>" + content)

    def git(self, folder: Path, *arguments: str) -> None:
        _ = subprocess.run(
            ["git", "-c", "user.name=Console Test", "-c", "user.email=console@example.invalid", *arguments],
            cwd=folder, check=True, capture_output=True, timeout=TEST_HANG_GUARD_SECS)

    def commit_fixture_files(self, folder: Path, subject: str) -> None:
        self.git(folder, "add", "--all", "--", ".", ":(exclude,glob)**/__pycache__/**")
        self.git(folder, "commit", "-m", subject)

    def legacy_console_base(self) -> Path:
        base = self.candidate.with_name("base")
        self.copy_candidate(base)
        production = base / "scripts/production"
        for name in ("console_browser.py", "console_check.py", "console_server.py", "console_settings.py",
                     "console_write_scope.py"):
            (production / name).unlink()
        return base

    def commit_console_base(self, base: Path) -> None:
        _ = (base / "README.md").write_text("Fixture base.\n")
        self.git(base, "init", "-b", "main")
        self.commit_fixture_files(base, "Create fixture base")

    def test_first_console_scans_the_candidate_with_an_empty_base(self) -> None:
        self.enterContext(self.http_visits(self.visit_fixture_notice))
        base = self.candidate.with_name("base")
        base.mkdir()
        self.commit_console_base(base)
        self.write_page("first", "<p data-error-notice>The first console has an error.</p>")
        with redirect_stdout(StringIO()) as output:
            refusal = console_check.compare_to_main(base, "HEAD", self.candidate)
        self.assertEqual(refusal, "RED: console check. First: The first console has an error.")
        self.assertNotIn("base console", output.getvalue())
        self.assertIn("candidate console First: red", output.getvalue())

    def test_legacy_console_scans_and_missing_candidate_config_refuses(self) -> None:
        self.enterContext(self.http_visits(self.visit_fixture_notice))
        base = self.legacy_console_base()
        self.commit_console_base(base)
        exported = self.candidate.with_name("exported")
        exported.mkdir()
        console_check.export_tree(base, "HEAD", exported)
        self.write_page("first", "<p data-error-notice>Both trees read the same notice.</p>")
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(console_check.run_check(self.candidate, base=exported), "", output.getvalue())
        self.assertIn("base console First: red", output.getvalue())
        self.assertIn("live console faults (not held) First: Both trees read the same notice.", output.getvalue())
        (self.candidate / "config/console.conf").unlink()
        self.assertEqual(console_check.run_check(self.candidate, base=exported),
                         "RED: console check. candidate console is missing required files: config/console.conf")

    @unittest.skipUnless(browser_available(), "Headless Chrome is unavailable.")
    def test_chrome_renders_all_clean_pages_including_rebuilds(self) -> None:
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(console_check.run_check(self.candidate), "", output.getvalue())
        self.assertIn("console First: ok", output.getvalue())
        self.assertIn("console Second: ok", output.getvalue())
        self.assertIn("console Rebuilds: ok", output.getvalue())

    @unittest.skipUnless(browser_available(), "Headless Chrome is unavailable.")
    def test_comparison_holds_only_new_page_faults(self) -> None:
        base = self.candidate.with_name("base")
        self.copy_candidate(base)
        settings = base / "config/console.conf"
        _ = settings.write_text(settings.read_text().replace(":first_page", ":base_first_page")
                                .replace(":second_page", ":base_second_page"))
        self.write_page("base-first", "<p data-error-notice>Could not load /live/records.md (old entry).</p>")
        self.write_page("first", "<p data-error-notice>Cannot read /live/records.md (new entry).</p>")
        self.write_page("base-second", "<h1>Page ready</h1>")
        self.write_page("second", "<p data-error-notice>Could not load /live/records.md.</p>")
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(console_check.run_check(self.candidate, base=base),
                             "RED: console check. Second: Could not load /live/records.md.", output.getvalue())
        self.assertIn("live console faults (not held) First: Cannot read /live/records.md", output.getvalue())
        self.assertEqual(output.getvalue().count("live console faults (not held)"), 1)

    def test_page_status_age_does_not_change_the_fault_identity(self) -> None:
        def result(age: int, reason: str) -> PageResult:
            fault = console_browser.FailedPageStatus("disks.refresh", "Failed", reason)
            detail = f"Measure now Failed {age} min ago {reason}"
            return PageResult(1, (PageFault(FaultKind.NOTICE, detail, fault),))

        base = result(430, "Failed to fetch")
        candidate = result(431, "Failed to fetch")
        changed = result(431, "The report is invalid")
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(self.check_render_attempts((candidate,) * 3, (base,) * 3), "")
            refusal = self.check_render_attempts((changed,) * 3, (base,) * 3, name="Second")
        self.assertIn("base console First: red", output.getvalue())
        self.assertIn("live console faults (not held) First: Measure now Failed 431 min ago Failed to fetch",
                      output.getvalue())
        self.assertEqual(refusal, "RED: console check. Second: Measure now Failed 431 min ago The report is invalid")

    def test_second_failed_component_with_same_reason_is_new(self) -> None:
        reason = "Failed to fetch"
        detail = f"Failed 1 min ago {reason}"
        primary = console_browser.FailedPageStatus("first.primary", "Failed", reason)
        secondary = console_browser.FailedPageStatus("first.secondary", "Failed", reason)
        first = PageFault(FaultKind.NOTICE, detail, primary)
        second = PageFault(FaultKind.NOTICE, detail, secondary)
        base = PageResult(1, (first,))
        candidate = PageResult(1, (first, second))
        with redirect_stdout(StringIO()) as output:
            refusal = self.check_render_attempts((candidate,) * 3, (base,) * 3)
        self.assertEqual(refusal, "RED: console check. First: Failed 1 min ago Failed to fetch")
        self.assertIn("new console faults First: Failed 1 min ago Failed to fetch [components: first.secondary]",
                      output.getvalue())

    @unittest.skipUnless(browser_available(), "Headless Chrome is unavailable.")
    def test_equal_status_text_preserves_each_component(self) -> None:
        self.write_page("first", failed_status("first.primary", "Failed to fetch")
                        + '<iframe src="/fixture/nested.html"></iframe>')
        self.write_page("nested", failed_status("first.secondary", "Failed to fetch"))
        self.write_page("second", failed_status("first.secondary", "Failed to fetch")
                        + '<iframe src="/fixture/other.html"></iframe>')
        self.write_page("other", failed_status("first.primary", "Failed to fetch"))
        with redirect_stdout(StringIO()) as output:
            refusal = console_check.run_check(self.candidate)
        self.assertIn("First: Failed 1 min ago Failed to fetch | Failed 1 min ago Failed to fetch", refusal)
        self.assertIn("Second: Failed 1 min ago Failed to fetch | Failed 1 min ago Failed to fetch", refusal)
        self.assertIn("[components: first.primary, first.secondary]", output.getvalue())
        self.assertIn("[components: first.secondary, first.primary]", output.getvalue())

    def test_configuration_status_age_does_not_change_identity(self) -> None:
        def result(age: int, reason: str) -> PageResult:
            fault = console_browser.FailedPageStatus("review.refresh", "Failed", reason)
            return PageResult(1, (PageFault(FaultKind.NOTICE, f"Failed {age} min ago {reason}", fault),))

        reason = "<console-config>/console.conf: age_limit='invalid'"
        changed_reason = "<console-config>/console.conf: age_limit='changed'"
        base = result(1, reason)
        candidate = result(2, reason)
        changed = result(2, changed_reason)
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(self.check_render_attempts((candidate,) * 3, (base,) * 3), "")
            refusal = self.check_render_attempts((changed,) * 3, (base,) * 3, name="Second")
        self.assertEqual(refusal, f"RED: console check. Second: Failed 2 min ago {changed_reason}")
        self.assertIn(f"live console faults (not held) First: Failed 2 min ago {reason}", output.getvalue())

    @unittest.skipUnless(browser_available(), "Headless Chrome is unavailable.")
    def test_browser_holds_notices_script_errors_and_saved_case_faults(self) -> None:
        self.write_page("first", "<p data-error-notice>Fixture records are unavailable.</p>")
        self.write_page("second", "<script>throw new Error('Fixture script broke')</script>")
        registry = self.candidate / "config/console.conf"
        _ = registry.write_text(registry.read_text()
                                + "page.accounts.label=Accounts\npage.accounts.title=Accounts\npage.accounts.icon=inbox\n"
                                + "page.accounts.url=python:scripts.production.test_console_fixtures:rebuilds_page\n")
        cases = self.candidate / console_check.CONSOLE_CASES
        cases.mkdir()
        self.write_page("replay", """<h1>Saved view</h1><script>
if (location.search === '?view=saved' && location.hash === '#saved-row' &&
    innerWidth === 744 && innerHeight === 690 && matchMedia('(prefers-color-scheme: dark)').matches)
  throw new Error('Saved view broke');
</script>""")
        _ = (cases / "B1.json").write_text(json.dumps({
            "item_key": "B1", "page": "fixture", "url": "http://fixture.invalid/fixture/replay.html?view=saved#saved-row",
            "viewport": {"width": 744, "height": 690, "dark": True},
        }))
        _ = (cases / "B2.json").write_text(json.dumps({
            "item_key": "B2", "page": "removed", "url": "http://fixture.invalid/console/?page=accounts",
            "viewport": {"width": 1280, "height": 800, "dark": False},
        }))
        with redirect_stdout(StringIO()) as output:
            refusal = console_check.run_check(self.candidate)
        self.assertTrue(refusal.startswith("RED: console check"), output.getvalue())
        self.assertIn("First: Fixture records are unavailable.", refusal)
        self.assertIn("Second: Error: Fixture script broke", refusal)
        self.assertIn("fixture (B1): Error: Saved view broke", refusal)
        self.assertIn("console Accounts: ok", output.getvalue())
        self.assertIn("removed (B2): The console did not select the requested page.", refusal)

    @unittest.skipUnless(browser_available(), "Headless Chrome is unavailable.")
    def test_failure_reaps_both_processes(self) -> None:
        self.write_page("first", "<p role=alert>Could not load fixture data.</p>")
        children: list[subprocess.Popen[bytes]] = []

        @contextmanager
        def capture(command: Sequence[str], *, cwd: Path, env: Mapping[str, str],
                    stderr: BinaryIO) -> Generator[subprocess.Popen[bytes]]:
            with managed_process(command, cwd=cwd, env=env, stderr=stderr) as child:
                children.append(child)
                yield child

        with patch.object(console_check, "managed_process", capture), redirect_stdout(StringIO()) as output:
            self.assertIn("Could not load fixture data.", console_check.run_check(self.candidate), output.getvalue())
        server, browser = children
        for child in (server, browser):
            with self.subTest(command=child.args), self.assertRaises(ProcessLookupError):
                os.kill(child.pid, 0)

    @unittest.skipUnless(browser_available(), "Headless Chrome is unavailable.")
    def test_checker_sigkill_stops_server_accounts_and_chrome_groups(self) -> None:
        self.page_function("page.first.url", "python:scripts.production.test_console_fixtures:startup_cleanup_page",
                           self.candidate)
        _ = self.configure_accounts_child()
        self.append_to_accounts_page(self.candidate,
            "from scripts.production.test_console_fixtures import wait_for_checker_exit\n    wait_for_checker_exit()", "b''")
        # Chrome needs short paths for its Unix sockets.
        scratch = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="console-kill-", dir="/tmp"))).resolve()

        def processes() -> list[list[str]]:
            result = subprocess.run(["ps", "-axww", "-o", "pid=,ppid=,pgid=,stat=,command="],
                                    capture_output=True, text=True, check=True, timeout=5)
            rows = [line.split(maxsplit=4) for line in result.stdout.splitlines()]
            return [row for row in rows if len(row) == 5 and row[0].isdigit() and row[2].isdigit()
                    and not row[3].startswith("Z")]

        command = [sys.executable, "-c", """
import signal
import sys
from pathlib import Path
from unittest.mock import patch

with patch.object(signal, "signal", side_effect=AssertionError("The checker installed a signal handler.")):
    from scripts.production import console_check
    from scripts.production.console_browser import Browser
    from scripts.production.test_console_fixtures import wait_for_checker_exit

    event = Browser.event
    def report_busy(browser, message):
        if (message.get("method") == "Runtime.consoleAPICalled"
                and "Fixture Chrome is busy" in repr(message)):
            wait_for_checker_exit()
        event(browser, message)

    with patch.object(Browser, "event", report_busy):
        refusal = console_check.run_check(Path(sys.argv[1]))
    sys.exit(refusal or 0)
""", str(self.candidate)]
        for stage in ("startup", "accounts", "chrome"):
            with self.subTest(stage=stage), socket.socket() as listener, tempfile.TemporaryFile() as log:
                if stage == "chrome":
                    self.write_page("first", "<script>console.log('Fixture Chrome is busy'); for (;;) {}</script>")
                listener.bind(("127.0.0.1", 0))
                listener.listen()
                listener.settimeout(45)
                _, port = cast(tuple[str, int], listener.getsockname())
                environment = {**os.environ, "TMPDIR": str(scratch.resolve()), "CONSOLE_TEST_CLEANUP_STAGE": stage,
                               "CONSOLE_TEST_CLEANUP_PORT": str(port)}
                groups: set[str] = set()
                with subprocess.Popen(command, cwd=REPOSITORY, env=environment, stdout=log, stderr=log) as checker:
                    try:
                        connection = listener.accept()[0]
                        with connection, connection.makefile("rb") as ready:
                            connection.settimeout(5)
                            active = int(ready.readline())
                            rows = processes()
                            groups = {pid for pid, parent, group, _, _ in rows
                                      if parent == str(checker.pid) and pid == group}
                            self.assertEqual(len(groups), 1 if stage == "startup" else 2)
                            server, = (row for row in rows if row[2] in groups and row[1] == row[2]
                                       and "console_server.py" in row[4])
                            if stage == "startup":
                                self.assertEqual(int(server[0]), active)
                            else:
                                browser, = (row for row in rows if row[2] in groups - {server[2]}
                                            and row[1] == row[2] and "--headless" in row[4])
                                self.assertTrue(any(row[2] == browser[2] and "--type=" in row[4] for row in rows))
                                if stage == "accounts":
                                    self.assertTrue(any(row[0] == str(active) and row[1] == server[0]
                                                        and row[2] == server[2] for row in rows), rows)
                                else:
                                    self.assertEqual(checker.pid, active)
                            checker.kill()
                            deadline = time.monotonic() + 5
                            self.assertEqual(checker.wait(timeout=5), -signal.SIGKILL)
                            while remaining := [row for row in processes() if row[2] in groups]:
                                self.assertLess(time.monotonic(), deadline, remaining)
                                time.sleep(0.05)
                    except TimeoutError as error:
                        _ = log.seek(0)
                        self.fail(f"{error}\n{log.read().decode(errors='replace')}")
                    finally:
                        if checker.poll() is None:
                            checker.kill()
                        for group in groups:
                            with suppress(ProcessLookupError):
                                os.killpg(int(group), signal.SIGKILL)

    def page_function(self, key: str, reference: str, tree: Path) -> None:
        settings = tree / "config/console.conf"
        lines = settings.read_text().splitlines()
        _ = settings.write_text("\n".join(f"{key}={reference}" if line.startswith(key + "=") else line
                                          for line in lines) + "\n")

    def configure_generated_folders(self) -> None:
        for name in ("disks", "memory_feed"):
            source = (REPOSITORY / f"config/{name}.conf").read_text().splitlines()
            folder = self.store / name
            config = self.candidate / f"config/{name}.conf"
            _ = config.write_text("\n".join(f"output_folder={folder}" if line.startswith("output_folder=") else line
                                            for line in source) + "\n")

    def configure_disks_resolver(self, key: str) -> None:
        self.configure_generated_folders()
        _ = (self.candidate / "config/build_folders.conf").write_text("# No slots in this fixture.\n")
        config = self.candidate / "config/console.conf"
        source = (REPOSITORY / "config/console.conf").read_text()
        page_status = source[source.index("# The shared page status"):]
        _ = config.write_text(config.read_text() + page_status)
        self.page_function(key, "python:scripts.disks.disks_page:page_url", self.candidate)

    def write_builds_feed(self) -> None:
        folder = self.store / "memory_feed"
        folder.mkdir()
        _ = (folder / "memory.json").write_text(json.dumps({
            "ok": True, "read_at": time.time(), "render_seconds": 0, "report_version": None,
            "total": 60, "colors": {}, "ram": {"missing": []}, "memory_status": {"code": "none"},
            "psi": {"some": 0, "full": 0}, "limits": {"some": 1, "full": 1}, "pressure": "none",
            "swap": {"used_bytes": 0, "capacity_bytes": 0, "outside_bytes": 0, "builds_ci_bytes": 0},
            "swap_by": [], "walk": [], "waiters": [],
        }))

    def write_disks_report(self, name: str) -> None:
        folder = self.store / name
        folder.mkdir()
        _ = (folder / "disks.json").write_text(json.dumps({
            "generation": 17, "measured_at": time.time(), "render_seconds": 0, "keep_bytes": 0, "drives": [],
        }))

    def test_rebuilds_disks_resolver_publishes_only_to_the_private_folder(self) -> None:
        self.configure_disks_resolver("rebuilds.url")
        folder = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="console-assets-")))
        assets = folder / "pages"
        settings = folder / "installed.json"
        console_settings.InstalledSettings.read().save(settings)
        environment = {**os.environ, "REVIEW_LINKS_HOST": "127.0.0.1"}
        command = console_check.server_command(REPOSITORY / console_check.SERVER_MODULE, assets, settings)
        with (folder / "server.log").open("wb") as log:
            with managed_process(command, cwd=self.candidate, env=environment, stderr=log) as child:
                assert child.stdout is not None
                ready = message_object(Pipe(cast(BinaryIO, child.stdout), b"\n").read(time.monotonic() + 15))
                self.assertIn({"identity": "rebuilds", "label": "Rebuilds", "path": "/disks/disks.html"},
                              cast(list[object], ready["pages"]))
                connection = HTTPConnection("127.0.0.1", cast(int, ready["port"]), timeout=5)
                try:
                    connection.request("GET", "/disks/disks.html")
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200, response.read())
                    _ = response.read()
                    attempt = "d" * 32
                    headers = {"X-Console-Attempt": attempt}
                    connection.request("GET", "/__console_check/resolve?page=rebuilds", headers=headers)
                    setup = connection.getresponse()
                    self.assertEqual(setup.status, 200, setup.read())
                    connection.request("GET", "/disks/disks.html", headers=headers)
                    retry = connection.getresponse()
                    self.assertEqual(retry.status, 200, retry.read())
                    self.assertTrue((assets / f"retry-{attempt}/disks/disks.html").is_file())
                finally:
                    connection.close()
                self.assertTrue((assets / "disks/disks.html").is_file())
                self.assertFalse((self.store / "disks").exists())

    def test_scans_resolve_disks_from_each_trees_settings(self) -> None:
        self.enterContext(self.http_visits())
        self.configure_disks_resolver("page.second.url")
        base = self.candidate.with_name("base")
        self.copy_candidate(base)
        settings = base / "config/disks.conf"
        _ = settings.write_text(settings.read_text().replace(f"output_folder={self.store / 'disks'}",
                                                              f"output_folder={self.store / 'base-disks'}"))
        self.write_builds_feed()
        self.write_disks_report("base-disks")
        self.write_disks_report("disks")
        observed: list[str] = []
        read_ready = console_check.ready_pages

        def record_paths(value: object) -> list[console_check.ReadyPage | console_check.PageSetupFault]:
            pages = read_ready(value)
            observed.extend(page["path"] for page in pages if page["identity"] == "second" and "path" in page)
            return pages

        with patch.object(console_check, "ready_pages", record_paths), redirect_stdout(StringIO()) as output:
            self.assertEqual(console_check.run_check(self.candidate, base=base), "", output.getvalue())
        self.assertIn("base console Second: ok", output.getvalue())
        self.assertIn("candidate console Second: ok", output.getvalue())
        self.assertCountEqual(observed, ["/base-disks/disks.html", "/disks/disks.html"])

    def test_candidate_disks_folder_outside_the_review_root_is_a_page_fault(self) -> None:
        self.enterContext(self.http_visits())
        self.configure_disks_resolver("page.second.url")
        base = self.candidate.with_name("base")
        self.copy_candidate(base)
        settings = self.candidate / "config/disks.conf"
        outside = self.candidate.parent / "outside"
        _ = settings.write_text(settings.read_text().replace(f"output_folder={self.store / 'disks'}",
                                                              f"output_folder={outside}"))
        self.write_builds_feed()
        self.write_disks_report("disks")
        # Both scans inherit the base configuration before the check selects a tree.
        with patch.dict(os.environ, {"CONF_DIR": str(base / "config")}), redirect_stdout(StringIO()) as output:
            refusal = console_check.run_check(self.candidate, base=base)
        self.assertIn("base console Second: ok", output.getvalue())
        self.assertIn("RED: console check. Second:", refusal)
        self.assertIn(str(outside / "disks.html"), refusal)
        self.assertIn("is not in the subpath", refusal)
        self.assertFalse(outside.exists())

    def test_both_scans_keep_live_delivery_size_and_running_job_identity(self) -> None:
        from scripts.agents import detached_runs
        from scripts.lib import session_folder
        from scripts.production import change_routing

        _ = self.configure_accounts_child()
        self.configure_generated_folders()
        installed = Path(os.environ["CONF_DIR"])
        candidate_disks = self.candidate / "config/disks.conf"
        _ = shutil.copyfile(candidate_disks, installed / "disks.conf")
        self.write_disks_report("disks")
        self.write_builds_feed()
        self.page_function("page.first.url", "python:scripts.production.test_console_fixtures:live_review_delivery_page",
                           self.candidate)
        self.page_function("rebuilds.url", "python:scripts.disks.disks_page:page_url", self.candidate)
        self.append_to_accounts_page(self.candidate, "from scripts.production.test_console_fixtures import live_accounts_job",
                                     "live_accounts_job()")
        base = self.legacy_console_base()
        self.commit_console_base(base)
        _ = candidate_disks.write_text(candidate_disks.read_text().replace(str(self.store / "disks"),
                                                                           str(self.store / "candidate-disks")))
        folder = self.candidate.parent / "live-sessions"
        (folder / "busy-session").mkdir(parents=True)
        build_folders = installed / "build_folders.conf"
        roots = build_folders.read_text()
        _ = build_folders.write_text(re.sub(r"(?m)^roots\.session=.*", f"roots.session={folder}", roots))
        mounted = measure.drive_path(folder, measure.read_mounts())
        report = self.store / "disks" / disks_page.DATA_FILE
        data = message_object(report.read_bytes())
        data["drives"] = [{"mount": str(mounted), "tree": {"size": 42, "children": [
            {"name": "busy-session", "size": 42, "children": []},
        ]}}]
        _ = report.write_text(json.dumps(data))
        list_path = self.store / "fixture" / "list.md"
        _ = list_path.write_text("Live Review list")
        _ = change_routing.delivery_path(self.store, list_path, "A1").write_text('{"feedback":"Fixture delivery"}')
        run = self.candidate.parent / "live-run"
        record = run / detached_runs.DETACHED_RUNS_DIR / "fixture.json"
        record.parent.mkdir(parents=True)

        def visit(browser: Browser, path: str, _viewport: Viewport, _limit: float,
                  key: str, _direct: str) -> PageResult:
            if key == "first":
                self.assertIn(b'<h1>Page ready</h1>', self.fetch(browser, first_page()))
            elif key == "second":
                self.assertIn(b"Fixture detached job is busy", self.fetch(browser, accounts_page.PAGE_PATH))
            elif key == "rebuilds":
                shell = self.fetch(browser, path)
                match = re.search(rb'data-url="(/[^"]+)/disks.html"', shell)
                assert match is not None, shell
                connection = HTTPConnection(browser.origin.removeprefix("http://"), timeout=TEST_HANG_GUARD_SECS)
                try:
                    connection.request("POST", match[1].decode() + "/measure", "")
                    reply = connection.getresponse()
                    payload = message_object(reply.read())
                    self.assertEqual(reply.status, 200, payload)
                    sessions = message_object(json.dumps(payload["sessions"]).encode())
                    self.assertEqual(sessions["root"], str(folder))
                    self.assertEqual(sessions["sessions"], [
                        {"name": "busy-session", "size": 42, "held": [], "state": "unknown"},
                    ])
                finally:
                    connection.close()
            return PageResult(0, ())

        with subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read()",
                               "detached_runs.py", "supervise", str(record)], stdin=subprocess.PIPE) as job:
            _ = record.write_text(json.dumps({"status": "running", "pid": job.pid,
                                              "staff_id": "fixture-director", "label": "Fixture detached job"}))
            try:
                with (patch.dict(os.environ, {session_folder.ROOT_ENV: str(folder), "CONSOLE_TEST_RUN": str(run),
                                              "CONSOLE_TEST_REVIEW_LIST": str(list_path)}),
                      self.http_visits(visit), redirect_stdout(StringIO()) as output):
                    self.assertEqual(console_check.run_check(self.candidate, base=base), "", output.getvalue())
                for label in ("base", "candidate"):
                    for page in ("First", "Second", "Rebuilds"):
                        self.assertIn(f"{label} console {page}: ok", output.getvalue())
            finally:
                assert job.stdin is not None
                job.stdin.close()

    def test_live_host_lookup_failure_is_a_page_fault(self) -> None:
        self.enterContext(self.http_visits())
        tailscale = self.candidate.parent / "tailscale"
        _ = tailscale.write_text("#!/bin/sh\nexit 20\n")
        tailscale.chmod(0o755)
        self.page_function("page.first.url", "python:scripts.production.test_console_fixtures:first_page_with_live_host",
                           self.candidate)
        with patch.dict(os.environ, {"PATH": f"{tailscale.parent}:{os.environ['PATH']}"}), redirect_stdout(StringIO()):
            _ = os.environ.pop("REVIEW_LINKS_HOST", None)
            refusal = console_check.run_check(self.candidate)
        self.assertIn("RED: console check. First:", refusal)
        self.assertIn("['tailscale', 'ip', '-4']' returned non-zero exit status 20", refusal)

    def test_ready_reply_requires_a_path_or_a_setup_fault(self) -> None:
        ready = {"identity": "first", "label": "First", "path": "/fixture/first.html"}
        fault = {"identity": "second", "label": "Second", "fault": "No page available."}
        self.assertEqual(console_check.ready_pages([ready, fault]), [ready, fault])
        with self.assertRaisesRegex(ValueError, "must give a path or a setup fault"):
            _ = console_check.ready_pages([{**ready, "fault": "Conflicting fault."}])

    def test_builds_resolver_failure_is_reported_and_matches_the_base(self) -> None:
        self.enterContext(self.http_visits())
        self.configure_generated_folders()
        base = self.candidate.with_name("base")
        self.copy_candidate(base)
        reference = "python:scripts.build_queue.memory_feed:page_url"
        for tree in (base, self.candidate):
            self.page_function("page.first.url", reference, tree)
        self.page_function("page.second.url", reference, self.candidate)
        with redirect_stdout(StringIO()) as output:
            refusal = console_check.run_check(self.candidate, base=base)
        self.assertIn("RED: console check. Second: Builds page is not available", refusal, output.getvalue())
        self.assertIn("live console faults (not held) First: Builds page is not available", output.getvalue())

    def test_both_scans_use_the_installed_review_store(self) -> None:
        config = self.candidate / "config/change_review.conf"
        _ = config.write_text(f"review_links_directory={self.store}\n")
        base = self.candidate.with_name("base")
        self.copy_candidate(base)
        installed = self.candidate.with_name("installed")
        _ = shutil.copytree(self.candidate / "config", installed)
        other = self.candidate.with_name("other-store")
        _ = shutil.copytree(self.store, other)
        _ = (other / "fixture/first.html").write_text("<p data-error-notice>The tree selected another store.</p>")
        _ = config.write_text(f"review_links_directory={other}\n")
        def visit(browser: Browser, path: str, viewport: Viewport, limit: float,
                  key: str, direct: str) -> PageResult:
            if key == "first":
                destination = self.registered_page(browser, path, key)
                self.assertIn(b"Page ready", self.fetch(browser, destination))
                return PageResult(0, ())
            return self.visit_over_http(browser, path, viewport, limit, key, direct)

        with patch.dict(os.environ, {"CONF_DIR": str(installed)}), self.http_visits(visit), redirect_stdout(StringIO()) as output:
            _ = os.environ.pop("REVIEW_LINKS_ROOT", None)
            self.assertEqual(console_check.run_check(self.candidate, base=base), "", output.getvalue())
        self.assertIn("base console First: ok", output.getvalue())
        self.assertIn("candidate console First: ok", output.getvalue())

    def test_settings_and_both_scans_recover_installed_review_root_from_git(self) -> None:
        base = self.candidate.with_name("base")
        self.copy_candidate(base)
        installed = self.candidate.with_name("installed")
        _ = shutil.copytree(self.candidate / "config", installed)
        live = installed / "change_review.conf"
        _ = live.write_text(f"review_links_directory={self.store}\n")
        self.git(installed, "init", "-b", "main")
        self.commit_fixture_files(installed, "Set the installed review root")
        other = self.candidate.with_name("other-store")
        _ = (self.candidate / "config/change_review.conf").write_text(f"review_links_directory={other}\n")
        for tree in (base, self.candidate):
            self.page_function("page.first.url",
                               "python:scripts.production.test_console_fixtures:missing_review_config_page", tree)

        def visit(browser: Browser, path: str, viewport: Viewport, limit: float,
                  key: str, direct: str) -> PageResult:
            if key == "first":
                destination = self.registered_page(browser, path, key)
                self.assertIn(b"Page ready", self.fetch(browser, destination))
                return PageResult(0, ())
            return self.visit_over_http(browser, path, viewport, limit, key, direct)

        with patch.dict(os.environ, {"CONF_DIR": str(installed)}):
            _ = os.environ.pop("REVIEW_LINKS_ROOT", None)
            for contents in ("# The key is missing.\n", "review_links_directory=\n"):
                _ = live.write_text(contents)
                settings = console_settings.InstalledSettings.read()
                _ = live.write_text(f"review_links_directory={other}\n")
                for tree in (base, self.candidate):
                    with (self.subTest(contents=contents, tree=tree.name),
                          patch.object(conf, "directory", conf.directory),
                          patch.object(conf, "read", conf.read),
                          patch.object(conf, "read_or_repository", conf.read_or_repository),
                          patch.object(conf, "_repository_path"),
                          patch.object(conf_reads, "read_text", conf_reads.read_text)):
                        _ = settings.select(tree)
                        recovered = conf.read("change_review").origin("review_links_directory", str)
                        self.assertIsInstance(recovered, conf.RecoveredValue)
                        root = change_routing.review_root()
                        self.assertEqual(root, self.store)
                        self.assertIn(b"Page ready", (root / "fixture/first.html").read_bytes())

            live.unlink()
            read = console_settings.InstalledSettings.read

            def restore_live() -> console_settings.InstalledSettings:
                result = read()
                _ = live.write_text(f"review_links_directory={other}\n")
                return result

            with (patch.object(console_settings.InstalledSettings, "read", restore_live),
                  self.http_visits(visit), redirect_stdout(StringIO()) as output):
                self.assertEqual(console_check.run_check(self.candidate, base=base), "", output.getvalue())
            self.assertIn("base console First: ok", output.getvalue())
            self.assertIn("candidate console First: ok", output.getvalue())

    def test_scans_preserve_defaults_without_conf_dir(self) -> None:
        self.enterContext(self.http_visits())
        self.page_function("page.first.url", "python:scripts.production.test_console_fixtures:first_page_with_repository_defaults",
                           self.candidate)
        base = self.candidate.with_name("base")
        self.copy_candidate(base)
        home = self.candidate.with_name("home")
        (home / ".claude/config").mkdir(parents=True)
        with patch.dict(os.environ, {"HOME": str(home)}), redirect_stdout(StringIO()) as output:
            _ = os.environ.pop("CONF_DIR", None)
            self.assertEqual(console_check.run_check(self.candidate, base=base), "", output.getvalue())
        self.assertIn("base console First: ok", output.getvalue())
        self.assertIn("candidate console First: ok", output.getvalue())

    def test_scans_recover_live_memory_output_folder(self) -> None:
        self.configure_disks_resolver("page.second.url")
        self.page_function("page.second.url", "python:scripts.build_queue.memory_feed:page_url", self.candidate)
        self.write_builds_feed()
        live_folder = self.store / "memory_feed"
        _ = (live_folder / "memory_live.html").write_text("<h1>Live Builds</h1>")
        base = self.candidate.with_name("base")
        self.copy_candidate(base)
        installed = self.candidate.with_name("installed")
        _ = shutil.copytree(self.candidate / "config", installed)
        self.git(installed, "init", "-b", "main")
        self.commit_fixture_files(installed, "Set the installed Builds folder")
        live = installed / "memory_feed.conf"
        _ = live.write_text(live.read_text().replace(f"output_folder={live_folder}\n", ""))
        other = self.store / "other-builds"
        settings = self.candidate / "config/memory_feed.conf"
        _ = settings.write_text(settings.read_text().replace(str(live_folder), str(other)))
        def visit(browser: Browser, path: str, viewport: Viewport, limit: float,
                  key: str, direct: str) -> PageResult:
            if key == "second":
                self.assertEqual(self.registered_page(browser, path, key), "/memory_feed/memory_live.html")
                self.assertNotIn(b"<h1>Live Builds</h1>", self.fetch(browser, "/memory_feed/memory_live.html"))
                self.assertIs(message_object(self.fetch(browser, "/memory_feed/memory.json"))["ok"], True)
                return PageResult(0, ())
            return self.visit_over_http(browser, path, viewport, limit, key, direct)

        with (patch.dict(os.environ, {"CONF_DIR": str(installed)}), self.http_visits(visit),
              redirect_stdout(StringIO()) as output):
            self.assertEqual(console_check.run_check(self.candidate, base=base), "", output.getvalue())
        self.assertIn("base console Second: ok", output.getvalue())
        self.assertIn("candidate console Second: ok", output.getvalue())
        self.assertFalse(other.exists())

    def test_aws_explicit_config_uses_git_history_in_both_scans(self) -> None:
        from scripts.aws_target import aws_page

        self.page_function("page.second.url", "python:scripts.aws_target.aws_page:page_url", self.candidate)
        console_config = self.candidate / "config/console.conf"
        page_status = (REPOSITORY / "config/console.conf").read_text()
        _ = console_config.write_text(console_config.read_text()
                                      + page_status[page_status.index("# The shared page status"):])
        base = self.candidate.with_name("base")
        self.copy_candidate(base)
        folder = self.store / "aws"
        folder.mkdir()
        _ = (folder / aws_page.PAGE_NAME).write_text("<h1>Live AWS page</h1>")
        installed = Path.home() / ".claude"
        config = installed / "config"
        _ = shutil.copytree(self.candidate / "config", config)
        _ = (config / "aws_page.conf").write_text(f"output_folder={folder}\n")
        settings = config / "aws_target.conf"
        defaults = (REPOSITORY / "config/aws_target.conf").read_text()
        self.assertIn("spend_pace_days=7", defaults)
        _ = settings.write_text(defaults.replace("spend_pace_days=7", "spend_pace_days=13"))
        self.git(installed, "init", "-b", "main")
        self.commit_fixture_files(installed, "Set the AWS spend pace in Git")
        _ = settings.write_text(defaults.replace("spend_pace_days=7\n", ""))
        repository = installed.with_name("defaults")
        (repository / "config").mkdir(parents=True)
        _ = (repository / "config/aws_target.conf").write_text(defaults)
        _ = shutil.copyfile(REPOSITORY / "config/cloud_gate.conf", repository / "config/cloud_gate.conf")
        for tree in (base, self.candidate):
            renderer = tree / "scripts/aws_target/aws_page.py"
            code = renderer.read_text()
            marker = "    spend = spend_data(read, now, configuration.whole('spend_pace_days'))"
            self.assertIn(marker, code)
            self.replace_script(renderer, code.replace(marker, marker + "\n"
                + "    assert configuration.whole('spend_pace_days') == 13\n"
                + "    assert conf.read('aws_target').whole('spend_pace_days') == 7"))
        rendered: list[bytes] = []

        def visit(browser: Browser, _path: str, _viewport: Viewport, _limit: float,
                  key: str, _direct: str) -> PageResult:
            if key == "second":
                rendered.append(self.fetch(browser, f"/aws/{aws_page.PAGE_NAME}"))
            return PageResult(0, ())

        with (patch.dict(os.environ, {"REVIEW_LINKS_HOST": "127.0.0.1"}), self.http_visits(visit),
              patch.object(console_settings, "installed_checkout", return_value=repository),
              redirect_stdout(StringIO()) as output):
            _ = os.environ.pop("CONF_DIR")
            self.assertEqual(conf.read(settings).whole("spend_pace_days"), 13)
            self.assertEqual(console_check.run_check(self.candidate, base=base), "", output.getvalue())
        self.assertEqual(len(rendered), 2)
        for page in rendered:
            self.assertIn(b"AWS spend unavailable", page)

    def test_shared_aws_config_fault_is_not_new(self) -> None:
        self.enterContext(self.http_visits())
        self.page_function("page.second.url", "python:scripts.aws_target.aws_page:page_url", self.candidate)
        live = self.candidate / "config/aws_page.conf"
        _ = live.write_text("output_folder=\n")
        base = self.candidate.with_name("base")
        self.copy_candidate(base)
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(console_check.run_check(self.candidate, base=base), "", output.getvalue())
        for label in ("base console Second: red", "candidate console Second: red", "live console faults (not held) Second"):
            self.assertIn(label, output.getvalue())
        self.assertIn(f"{live}: output_folder=''", output.getvalue())

    def test_shared_invalid_disks_setting_is_not_new(self) -> None:
        self.enterContext(self.http_visits())
        self.configure_disks_resolver("page.second.url")
        settings = self.candidate / "config/disks.conf"
        _ = settings.write_text(settings.read_text().replace("page.poll_seconds=5", "page.poll_seconds=invalid"))
        base = self.candidate.with_name("base")
        self.copy_candidate(base)
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(console_check.run_check(self.candidate, base=base), "", output.getvalue())
        for label in ("base console Second: red", "candidate console Second: red", "live console faults (not held) Second"):
            self.assertIn(label, output.getvalue())
        self.assertIn("<console-config>/disks.conf: page.poll_seconds='invalid'", output.getvalue())

    def test_new_invalid_disks_value_refuses_even_with_committed_settings(self) -> None:
        self.enterContext(self.http_visits())
        self.configure_disks_resolver("page.second.url")
        self.write_builds_feed()
        self.write_disks_report("disks")
        base = self.candidate.with_name("base")
        self.copy_candidate(base)
        self.git(self.candidate, "init", "-b", "main")
        self.commit_fixture_files(self.candidate, "Set valid page settings")
        settings = self.candidate / "config/disks.conf"
        valid = settings.read_text()
        _ = settings.write_text(valid.replace("page.poll_seconds=5", "page.poll_seconds=candidate-invalid"))
        for base_value in ("5", "base-invalid"):
            with self.subTest(base_value=base_value), redirect_stdout(StringIO()):
                _ = (base / "config/disks.conf").write_text(valid.replace("page.poll_seconds=5",
                                                                         f"page.poll_seconds={base_value}"))
                refusal = console_check.run_check(self.candidate, base=base)
                self.assertIn("RED: console check. Second:", refusal)
                self.assertIn("<console-config>/disks.conf: page.poll_seconds='candidate-invalid'", refusal)
                self.assertNotIn("base-invalid", refusal)

    def configure_accounts_child(self) -> Path:
        self.page_function("page.second.url", "python:scripts.review_links.accounts_page:page_url", self.candidate)
        settings = self.candidate / "config/console.conf"
        source = (REPOSITORY / "config/console.conf").read_text()
        _ = settings.write_text(settings.read_text() + source[source.index("# The shared page status"):])
        (self.store / "console").mkdir()
        _ = (self.store / "console" / change_browser.MANIFEST).write_text('{"token":"fixture","initial":""}')
        installed = self.candidate.with_name("installed")
        _ = shutil.copytree(self.candidate / "config", installed)
        _ = config_environment(installed)
        self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(installed)}))
        return settings

    @contextmanager
    def http_visits(self, visit: BrowserVisit | None = None) -> Generator[None]:
        pipe = [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"]
        http_visit = self.visit_over_http if visit is None else visit

        def navigate(browser: Browser, path: str, viewport: Viewport, limit: float,
                     key: str, direct: str) -> PageResult:
            return http_visit(browser, path, viewport, limit, key, direct)

        with (patch.object(console_check, "browser_command", return_value=pipe), patch.object(Browser, "visit", navigate)):
            yield

    def visit_fixture_notice(self, browser: Browser, path: str, _viewport: Viewport,
                             _limit: float, key: str, _direct: str) -> PageResult:
        destination = self.registered_page(browser, path, key)
        content = self.fetch(browser, destination).decode()
        notice = re.search(r"<p data-error-notice>([^<]+)</p>", content)
        if notice is not None:
            return PageResult(0, (console_browser.notice_fault(notice[1]),))
        return PageResult(0, ())

    def read_accounts_on_visit(self, responses: list[bytes]) -> BrowserVisit:
        def read(browser: Browser, path: str, viewport: Viewport, limit: float,
                 page_key: str, direct: str) -> PageResult:
            if page_key == "second":
                destination = self.registered_page(browser, path, page_key)
                self.assertEqual(destination, accounts_page.PAGE_PATH)
                responses.append(self.fetch(browser, destination))
                return PageResult(0, ())
            return self.visit_over_http(browser, path, viewport, limit, page_key, direct)
        return read

    def registered_page(self, browser: Browser, shell: str, identity: str) -> str:
        document = self.fetch(browser, shell).decode()
        page = re.search(rf'data-page="{re.escape(identity)}"[^>]*data-url="([^"]+)"', document)
        if page is None:
            raise AssertionError(f"The shell has no available page for {identity}.")
        return unescape(page[1])

    def visit_over_http(self, browser: Browser, path: str, _viewport: Viewport, _limit: float,
                        key: str, _direct: str) -> PageResult:
        destination = self.registered_page(browser, path, key)
        status, _, _ = self.request(browser, destination)
        if status >= 400:
            kind = FaultKind.HTTP_SERVER if status >= 500 else FaultKind.HTTP_DOCUMENT
            return PageResult(0, (PageFault(kind, f"HTTP {status} {destination}", destination),))
        self.assertEqual(status, 200, destination)
        return PageResult(0, ())

    def request(self, browser: Browser, path: str) -> tuple[int, dict[str, str], bytes]:
        connection = HTTPConnection(browser.origin.removeprefix("http://"), timeout=TEST_HANG_GUARD_SECS)
        try:
            connection.request("GET", path, headers={"X-Console-Attempt": browser.attempt_id})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def fetch(self, browser: Browser, path: str) -> bytes:
        status, _, content = self.request(browser, path)
        self.assertEqual(status, 200, path)
        return content

    def append_to_accounts_page(self, tree: Path, prepare: str, expression: str) -> None:
        renderer = tree / "scripts/review_links/accounts_page.py"
        contents = renderer.read_text().replace("_ = sys.stdout.buffer.write(page())",
                                               prepare + f"\n    _ = sys.stdout.buffer.write(page() + ({expression}))")
        self.replace_script(renderer, contents)

    def test_server_and_accounts_child_block_outside_writes_except_read_log_appends(self) -> None:
        _ = self.configure_accounts_child()
        base = self.legacy_console_base()
        outside = self.candidate.parent / "outside"
        outside.mkdir()
        for name in ("open.txt", "read_bytes.txt", "fspath.txt"):
            _ = (outside / name).write_text("live")
        (outside / "list").mkdir()
        database = outside / "outside.db"
        connection = sqlite3.connect(database)
        try:
            _ = connection.execute("PRAGMA journal_mode=WAL")
            _ = connection.execute("CREATE TABLE retained (value INTEGER)")
        finally:
            connection.close()
        saved_database = database.read_bytes()
        model_cache = outside / "session-models.json"
        _ = model_cache.write_text("Model cache fixture.")
        for tree in (base, self.candidate):
            account_script = tree / "scripts/accounts/accounts.py"
            self.replace_script(account_script, account_script.read_text() + """
class _PersistentModelScans:
    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> str:
        return self.path.read_text()

    def save(self) -> None:
        self.path.write_text("The Accounts child tried to save the model cache.")
""")
            script = tree / "scripts/review_links/serve.py"
            contents = script.read_text()
            marker = "        route = urlsplit(self.path)"
            self.assertIn(marker, contents)
            self.replace_script(script, contents.replace(
                marker, '        if self.path == "/fixture/first.html":\n'
                + '            from scripts.production.test_console_fixtures import barrier_probes\n'
                + '            _ = barrier_probes("server")\n' + marker, 1))
            self.append_to_accounts_page(tree, "from scripts.production.test_console_fixtures import barrier_probes",
                                         'barrier_probes("Accounts child")')
        self.commit_console_base(base)

        def visit(browser: Browser, _path: str, _viewport: Viewport, _limit: float,
                  key: str, _direct: str) -> PageResult:
            if key == "first":
                _ = self.fetch(browser, first_page())
            elif key == "second":
                self.assertIn(b"Outside writes denied", self.fetch(browser, accounts_page.PAGE_PATH))
            return PageResult(0, ())

        start = console_check.start_console

        def start_with_links(stack: ExitStack, tree: Path, label: str, folder: Path, settings: Path,
                             chrome: list[str]) -> console_check.ConsoleSession:
            target = folder / "private-target"
            _ = target.write_text("private")
            outside_link = outside / f"{label}-outside-link"
            outside_link.symlink_to(target)
            (folder / "inside-link").symlink_to(outside / "open.txt")
            read_log = outside / f"{label}-reads"
            _ = read_log.write_text("first existing record\nsecond existing record\n")
            (folder / "read-log-link").symlink_to(read_log)
            with patch.dict(os.environ, {"CONSOLE_TEST_OUTSIDE_LINK": str(outside_link), "CONF_READ_LOG": str(read_log)}):
                return start(stack, tree, label, folder, settings, chrome)

        with (patch.dict(os.environ, {"CONSOLE_TEST_OUTSIDE": str(outside)}),
              patch.object(console_check, "start_console", start_with_links), self.http_visits(visit),
              redirect_stdout(StringIO()) as output):
            refusal = console_check.compare_to_main(base, "HEAD", self.candidate)
        for label in ("base", "candidate"):
            self.assertIn(f"{label} console First: ok", output.getvalue(), refusal)
            self.assertIn(f"{label} console Second: ok", output.getvalue(), refusal)
            self.assertTrue((outside / f"{label}-outside-link").is_symlink())
            read_log = outside / f"{label}-reads"
            records = read_log.read_text().splitlines()
            self.assertEqual(records[:2], ["first existing record", "second existing record"])
            for process in ("server", "Accounts child"):
                self.assertEqual(records.count(f"{process} fixture read"), 1)
                inside_link = (outside / f"{label}-outside-link").readlink().with_name("inside-link")
                for operation, name in (("os.remove", str(outside / f"{label}-outside-link")), ("open", str(inside_link))):
                    self.assertIn(f"{label} console filesystem barrier. {process} {operation}: {name!r}", refusal)
                self.assertIn(f"{label} console filesystem barrier. {process} open: b'inside-link' (dir_fd=", refusal)
                for operation in ("open", "os.truncate", "os.remove", "os.rename", "os.link"):
                    self.assertIn(f"{label} console filesystem barrier. {process} {operation}: {str(read_log)!r}", refusal)
                read_log_link = inside_link.with_name("read-log-link")
                self.assertIn(f"{label} console filesystem barrier. {process} open: {str(read_log_link)!r}", refusal)
                self.assertIn(f"{label} console filesystem barrier. {process} open: {read_log.name!r} (dir_fd=", refusal)
                for operation, name in (("open", "write.txt"), ("os.rmdir", "list"), ("os.mkdir", "write-dir"),
                                        ("os.chmod", "open.txt"), ("os.utime", "open.txt"), ("os.rename", "open.txt"),
                                        ("os.chown", "open.txt"), ("os.setxattr", "open.txt"),
                                        ("os.removexattr", "open.txt")):
                    self.assertIn(f"{label} console filesystem barrier. {process} {operation}: {str(outside / name)!r}",
                                  refusal)
                for name in (database, database.as_uri() + "?mode=ro", ":memory:"):
                    self.assertIn(f"{label} console filesystem barrier. {process} sqlite3.connect: {name!r}", refusal)
                for operation, name in (("open", "write-at.txt"), ("os.remove", "open.txt"), ("os.chown", "open.txt")):
                    self.assertIn(f"{label} console filesystem barrier. {process} {operation}: {name!r} (dir_fd=",
                                  refusal)
        self.assertEqual((outside / "open.txt").read_bytes(), b"live")
        self.assertFalse((outside / "write-at.txt").exists())
        self.assertFalse((outside / "write.txt").exists())
        self.assertEqual(database.read_bytes(), saved_database)
        self.assertEqual(model_cache.read_text(), "Model cache fixture.")
        self.assertNotIn("session-models.json", refusal)
        for suffix in ("-shm", "-wal"):
            self.assertFalse(database.with_name(database.name + suffix).exists())

    def test_an_ordinary_warning_with_the_old_routing_text_does_not_hold(self) -> None:
        self.enterContext(self.http_visits())
        self.page_function("page.first.url", "python:scripts.production.test_console_fixtures:ordinary_warning_page",
                           self.candidate)
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(console_check.run_check(self.candidate), "", output.getvalue())
        self.assertIn("candidate console First: ok", output.getvalue())

    def accounts_role_fixture(self) -> tuple[Path, Path, Path]:
        _ = self.configure_accounts_child()
        home = self.candidate.with_name("roles-home")
        self.enterContext(patch.dict(os.environ, {"HOME": str(home)}))
        workers = home / ".claude/config/accounts.json"
        workers.parent.mkdir(parents=True)
        _ = workers.write_text('{"workers":"codex1"}')
        groups = home / ".local/state/accounts/groups.json"
        groups.parent.mkdir(parents=True)
        _ = groups.write_text('{"units":"claude1"}')
        base = self.candidate.with_name("base")
        self.copy_candidate(base)
        return base, workers, groups

    def add_accounts_role_probe(self, tree: Path, expression: str) -> None:
        renderer = tree / "scripts/review_links/accounts_page.py"
        contents = renderer.read_text().replace(
            "_ = sys.stdout.buffer.write(page())",
            "from scripts.accounts import role_map\n"
            + "    roles = role_map.read_role_map()\n"
            + f"    _ = sys.stdout.buffer.write(page() + ({expression}))")
        self.replace_script(renderer, contents)

    def test_both_accounts_children_read_the_live_roles(self) -> None:
        base, _workers, _groups = self.accounts_role_fixture()
        for tree in (base, self.candidate):
            self.add_accounts_role_probe(tree, "json.dumps([roles.workers, roles.groups['units']]).encode()")

        observed: list[bytes] = []
        with self.http_visits(self.read_accounts_on_visit(observed)), redirect_stdout(StringIO()) as output:
            refusal = console_check.run_check(self.candidate, base=base)
        self.assertEqual(refusal, "", output.getvalue())
        self.assertEqual(len(observed), 2)
        self.assertTrue(all(page.endswith(b'["codex1", "claude1"]') for page in observed))

    def test_both_accounts_children_use_the_live_cache_and_home_cutover(self) -> None:
        base, workers, groups = self.accounts_role_fixture()
        cache = groups.with_name("role_map_cache.json")
        original = '{"workers":"codex1","home_cutovers":[{"at":"2026-01-01T00:00:00+00:00","label":"claude1"}]}'
        cached = '{"at":"2026-01-01T00:00:00+00:00","map":{"groups":{},"workers":"codex2"}}'
        _ = workers.write_text(original)
        _ = cache.write_text(cached)
        for tree in (base, self.candidate):
            self.append_to_accounts_page(tree, "from scripts.production.test_console_fixtures import cached_role_and_cutover",
                                         "cached_role_and_cutover()")

        observed: list[bytes] = []
        with self.http_visits(self.read_accounts_on_visit(observed)), redirect_stdout(StringIO()) as output:
            refusal = console_check.run_check(self.candidate, base=base)
        self.assertEqual(refusal, "", output.getvalue())
        self.assertEqual(len(observed), 2)
        self.assertTrue(all(page.endswith(b'["codex2", "claude1"]') for page in observed))

    def test_first_candidate_role_fault_holds_after_the_live_role_changes(self) -> None:
        base, workers, _groups = self.accounts_role_fixture()
        self.add_accounts_role_probe(
            self.candidate, "b'<p data-error-notice>The candidate cannot render codex1.</p>' "
            + "if roles.workers == 'codex1' else b''")

        def change_after_fault(browser: Browser, path: str, viewport: Viewport, limit: float,
                               key: str, direct: str) -> PageResult:
            result = self.visit_fixture_notice(browser, path, viewport, limit, key, direct)
            if any("cannot render codex1" in fault.detail for fault in result.faults):
                _ = workers.write_text('{"workers":"codex2"}')
            return result

        with self.http_visits(change_after_fault), redirect_stdout(StringIO()) as output:
            refusal = console_check.run_check(self.candidate, base=base)
        self.assertIn("candidate console Second: ok", output.getvalue())
        self.assertIn("base console Second: ok", output.getvalue())
        self.assertEqual(refusal, "RED: console check. Second: The candidate cannot render codex1.")

    @unittest.skipUnless(browser_available(), "Headless Chrome is unavailable.")
    @unittest.skipUnless(browser_available(), "Headless Chrome is unavailable.")
    def test_legacy_status_keys_apply_in_both_processes_and_invalid_keys_hold(self) -> None:
        registry = self.configure_accounts_child()
        _ = registry.write_text(registry.read_text()
                                + "page.accounts.label=Accounts\npage.accounts.title=Accounts\n"
                                + "page.accounts.icon=inbox\n"
                                + "page.accounts.url=python:scripts.review_links.accounts_page:page_url\n")
        base = self.candidate.with_name("base")
        self.copy_candidate(base)
        frame = base / "scripts/review_links/page_frame.py"
        contents = "".join(line for line in frame.read_text().splitlines(keepends=True)
                           if "data-status-component=" not in line)
        self.replace_script(frame, contents)
        self.page_function("page.first.url", "python:scripts.production.test_console_fixtures:legacy_server_page", base)
        self.page_function("page.first.url", "python:scripts.production.test_console_fixtures:base_first_page", self.candidate)
        self.write_page("base-first", "<h1>Candidate fixture</h1>")
        self.write_page("base-second", "<h1>Base fixture</h1>")
        self.write_page("second", failed_status("fixture.refresh", "One") + '<iframe src="/fixture/duplicate.html"></iframe>')
        self.write_page("duplicate", failed_status("fixture.refresh", "Two"))
        self.page_function("page.second.url", "python:scripts.production.test_console_fixtures:base_second_page", base)
        self.page_function("page.second.url", "python:scripts.production.test_console_fixtures:second_page", self.candidate)

        def check_invalid_keys(sessions: list[console_check.ConsoleSession], _cases: list[console_check.PageVisit],
                               limit: float) -> list[str]:
            self.assertEqual([session.label for session in sessions], ["base", "candidate"])
            for session in sessions:
                browser = session.browser
                path = self.registered_page(browser, "/console/?page=accounts", "accounts")
                self.assertEqual(path, accounts_page.PAGE_PATH)
                page = self.fetch(browser, path)
                self.assertIn(b'data-status-component="accounts.refresh"', page)
            faults: list[str] = []
            with ThreadPoolExecutor(max_workers=2) as workers:
                first = console_check.PageVisit("First", "/console/?page=first", "first", console_check.VisitKind.REGISTRY)
                with self.http_visits():
                    self.assertEqual(console_check.check_page(sessions, first, limit, workers), [])
                self.write_page("first", failed_status("", "Legacy fixture"))
                for key, label, detail in (
                    ("first", "First", "base console First: Page status has no valid component key"),
                    ("second", "Second", "candidate console Second: Page status component key is ambiguous: fixture.refresh"),
                ):
                    visit = console_check.PageVisit(label, f"/console/?page={key}", key, console_check.VisitKind.REGISTRY)
                    with self.subTest(key=key):
                        with self.assertRaisesRegex(ValueError, detail) as failure:
                            _ = console_check.check_page(sessions, visit, limit, workers)
                        faults.append(str(failure.exception))
            return faults

        with patch.object(console_check, "check_sessions", check_invalid_keys), redirect_stdout(StringIO()) as output:
            refusal = console_check.run_check(self.candidate, base=base)
        self.assertIn("base console First: ok", output.getvalue())
        self.assertEqual(refusal,
                         "RED: console check. base console First: Page status has no valid component key: "
                         + "Failed 1 min ago Legacy fixture"
                         + " | candidate console Second: Page status component key is ambiguous: fixture.refresh")

    def copy_distinct_config_readers(self, *trees: Path) -> None:
        for tree in trees:
            library = tree / "scripts/lib"
            shutil.rmtree(library)
            _ = shutil.copytree(REPOSITORY / "scripts/lib", library, symlinks=True,
                                ignore=shutil.ignore_patterns("__pycache__"))
            self.assertFalse((library / "conf.py").is_symlink())

    def test_missing_live_root_uses_installed_recovery_in_both_scans(self) -> None:
        home = self.candidate.with_name("home")
        installed = home / ".claude"
        config = installed / "config"
        config.mkdir(parents=True)
        _ = config_environment(config)
        installed_reader = installed / "scripts/lib/conf.py"
        installed_reader.parent.mkdir(parents=True)
        _ = shutil.copyfile(REPOSITORY / "scripts/lib/conf.py", installed_reader)
        base = self.candidate.with_name("base")
        self.copy_candidate(base)
        self.copy_distinct_config_readers(base, self.candidate)
        lives = config / "change_review.conf"
        _ = lives.write_text(f"review_links_directory={self.store}\n")
        self.git(installed, "init", "-b", "main")
        self.commit_fixture_files(installed, "Set the installed Review store")
        _ = lives.write_text("# Recover the missing installed root.\n")
        self.write_page("first", "<h1>Installed Review marker</h1>")
        for tree in (base, self.candidate):
            other = tree / "other-store"
            _ = shutil.copytree(self.store, other)
            _ = (other / "fixture/first.html").write_text(f"<h1>{tree.name} marker</h1>")
            _ = (tree / "config/change_review.conf").write_text(f"review_links_directory={other}\n")

        observed: list[bytes] = []

        def visit(browser: Browser, _path: str, _viewport: Viewport, _limit: float,
                  key: str, _direct: str) -> PageResult:
            if key == "first":
                observed.append(self.fetch(browser, first_page()))
            return PageResult(0, ())

        with (patch.dict(os.environ, {"HOME": str(home)}), self.http_visits(visit),
              redirect_stdout(StringIO()) as output):
            _ = os.environ.pop("CONF_DIR")
            _ = os.environ.pop("REVIEW_LINKS_ROOT")
            self.assertEqual(console_check.run_check(self.candidate, base=base), "", output.getvalue())
        self.assertEqual(len(observed), 2)
        for page in observed:
            self.assertIn(b"Installed Review marker", page)

    def test_live_settings_remain_fixed_between_scans(self) -> None:
        _ = self.configure_accounts_child()
        installed = Path(os.environ["CONF_DIR"])
        base = self.candidate.with_name("base")
        self.copy_candidate(base)
        other = self.candidate.with_name("other-store")
        _ = shutil.copytree(self.store, other)
        _ = (other / "fixture/first.html").write_text("<h1>Changed Review store</h1>")
        self.write_page("first", "<h1>Original Review store</h1>")
        live = installed / "change_review.conf"
        _ = live.write_text(f"review_links_directory={self.store}\n")
        accounts = installed / "accounts.conf"
        account_text = accounts.read_text()
        _ = account_text.index("usage_stale_minutes=10")
        for tree in (base, self.candidate):
            renderer = tree / "scripts/review_links/accounts_page.py"
            contents = renderer.read_text().replace(
                "sys.stdout.buffer.write(page())",
                "sys.stdout.buffer.write(page() + str(conf.read('accounts').whole('usage_stale_minutes')).encode())")
            self.replace_script(renderer, contents)

        stores: list[bytes] = []
        accounts_pages: list[bytes] = []

        def visit(browser: Browser, _path: str, _viewport: Viewport, _limit: float,
                  key: str, _direct: str) -> PageResult:
            if key == "first":
                stores.append(self.fetch(browser, first_page()))
            if key == "second":
                accounts_pages.append(self.fetch(browser, accounts_page.PAGE_PATH))
            return PageResult(0, ())

        read_settings = console_settings.InstalledSettings.read

        def read_then_change() -> console_settings.InstalledSettings:
            result = read_settings()
            _ = live.write_text(f"review_links_directory={other}\n")
            _ = accounts.write_text(account_text.replace("usage_stale_minutes=10", "usage_stale_minutes=11"))
            return result

        with (patch.dict(os.environ), self.http_visits(visit),
              patch.object(console_settings.InstalledSettings, "read", read_then_change), redirect_stdout(StringIO()) as output):
            _ = os.environ.pop("REVIEW_LINKS_ROOT")
            self.assertEqual(console_check.run_check(self.candidate, base=base), "", output.getvalue())
        self.assertEqual(len(stores), 2)
        self.assertEqual(len(accounts_pages), 2)
        for store, page in zip(stores, accounts_pages, strict=True):
            self.assertIn(b"Original Review store", store)
            self.assertTrue(page.endswith(b"10"))

    def test_rebuild_cache_read_ignores_unused_settings(self) -> None:
        _ = self.configure_accounts_child()
        self.page_function("rebuilds.url", "python:scripts.review_links.change_page:rebuilds_url", self.candidate)
        cache = self.candidate.with_name("rebuild-cache")
        _ = cache.write_text("rebuild natedev\n9999999999\n")
        config = Path(os.environ["CONF_DIR"]) / "statusline.conf"
        contents = config.read_text()
        observed: list[bytes] = []

        def visit(browser: Browser, _path: str, _viewport: Viewport, _limit: float,
                  key: str, _direct: str) -> PageResult:
            if key == "rebuilds":
                observed.append(self.fetch(browser, f"/{change_page.CONSOLE_FOLDER}/{change_page.REBUILDS_NAME}"))
            return PageResult(0, ())

        for missing in ("rebuild.generations", "rebuild.cwd.natedev", "rebuild.cwd.mac"):
            with self.subTest(missing=missing), self.http_visits(visit), redirect_stdout(StringIO()) as output:
                _ = config.write_text("\n".join(f"rebuild.cache={cache}" if line.startswith("rebuild.cache=") else line
                                              for line in contents.splitlines() if not line.startswith(missing + "=")))
                self.assertEqual(console_check.run_check(self.candidate), "", output.getvalue())
                self.assertIn(b'<article data-machine="natedev">', observed[-1])
        self.assertEqual(len(observed), 3)

    def test_both_trees_use_shared_settings_and_real_data_paths(self) -> None:
        self.maxDiff: int | None = None
        self.configure_generated_folders()
        candidate = self.configure_accounts_child()
        installed = Path(os.environ["CONF_DIR"])
        base = self.candidate.with_name("base")
        self.copy_candidate(base)
        self.copy_distinct_config_readers(base, self.candidate)
        self.write_disks_report("installed-disks")
        for tree, age in ((base, 47), (self.candidate, 49)):
            self.page_function("page.first.url", "python:scripts.production.test_console_fixtures:installed_inputs_page", tree)
            renderer = tree / "scripts/review_links/accounts_page.py"
            contents = renderer.read_text().replace(
                "_ = sys.stdout.buffer.write(page())",
                "from scripts.production.test_console_fixtures import live_input_values\n"
                + "    _ = sys.stdout.buffer.write(page() + b'<!--settings=' + json.dumps(live_input_values()).encode() + b'-->')")
            self.replace_script(renderer, contents)
            source = tree / "config/console.conf"
            _ = source.write_text(source.read_text().replace("status.accounts.age_limit_seconds=45",
                                                             f"status.accounts.age_limit_seconds={age}")
                                  + f"for_you.host={tree.name}\n"
                                  + "page.disks.label=Disks\npage.disks.title=Disks\npage.disks.icon=inbox\n"
                                  + "page.disks.url=python:scripts.disks.disks_page:page_url\n")
            disks = tree / "config/disks.conf"
            _ = disks.write_text(disks.read_text().replace(str(self.store / "disks"), str(self.store / f"{tree.name}-disks")))
            for stem, content in (("aws_page", f"output_folder={tree.name}-aws"),
                                  ("production", (REPOSITORY / "config/production.conf").read_text()
                                   .replace("mac_run_log=/tmp/mac_run_hana.log", f"mac_run_log={tree.name}-log")),
                                  ("cloud_gate", f"regions={tree.name}"),
                                  ("memory_feed", f"output_folder={tree.name}-builds")):
                _ = (tree / f"config/{stem}.conf").write_text(content + "\n")

        host = socket.gethostname().partition(".")[0]
        root = self.candidate.parent
        account_file = installed / "accounts.conf"
        account_text = account_file.read_text()
        roles = Path.home() / ".claude/config/accounts.json"
        roles.parent.mkdir(parents=True)
        _ = roles.write_text('{"workers":"codex1"}')
        _ = account_file.write_text(account_text.replace("usage_stale_minutes=10", "usage_stale_minutes=32"))
        self.git(installed, "init", "-b", "main")
        self.commit_fixture_files(installed, "Set the Accounts usage age")
        _ = account_file.write_text(account_text.replace("usage_stale_minutes=10", "usage_stale_minutes=invalid"))
        checkout = root / "installed-checkout"
        checkout_config = checkout / "config"
        checkout_config.mkdir(parents=True)
        (installed / "build_admission.conf").unlink()
        _ = (checkout_config / "build_admission.conf").write_text("ci_label=installed-ci\n")
        raw_rebuilds = {"rebuild.cache": str(root / "cache"), "rebuild.cwd.natedev": "/installed",
                        "rebuild.cwd.mac": "~/installed-mac", "rebuild.repo": "/installed", "rebuild.ref": "main",
                        "rebuild.natedev": "natedev", "rebuild.mac": "mac", "rebuild.ssh": "mac", "rebuild.interval": "60",
                        "rebuild.eval_timeout": "30", "rebuild.eval_retry_seconds": "10", "rebuild.generations": "~/log"}
        mac_paths = {"mac_run_log": str(Path.cwd() / "installed.log"), "mac_run_launch": "/installed/launch",
                     "mac_run_status": "/installed/status", "mac_run_bundle": "",
                     "mac_run_example_log_prefix": "installed-prefix", "mac_run_source": "installed-source"}
        machines = installed / "machines.conf"
        machine_text = machines.read_text()
        machine_settings = conf.settings(machine_text)
        aliases = machine_settings["aliases"].value.split()
        names = {host.casefold(), "installed-machine", "fixture-machine", *aliases}
        for alias in aliases:
            names.update(name.partition(".")[0].casefold() for name in machine_settings[f"{alias}.hosts"].value.split())
        _ = machines.write_text(machine_text.replace("aliases=" + " ".join(aliases),
                                                     "aliases=" + " ".join([*aliases, "installed-machine"]))
                                + "installed-machine.hosts=fixture-machine.local\ninstalled-machine.platform=linux\n")
        for stem, content in {
            "aws_page": f"output_folder={Path.cwd() / 'installed-aws'}",
            "aws_target": "# whole: 1..\nspend_pace_days=12",
            "build_folders": f"roots.session={root / 'installed-sessions'}",
            "build_steps": "names.integrate=installed step",
            "change_review": "review_links_directory=/unused-configured-store",
            "cloud_gate": "regions=us-east-1",
            "codex_daemon": "# range: 0.001..\ntimeout_seconds=2.5",
            "console": f"for_you.host=unused-host\nfor_you.host.{host}=installed-host",
            "disks": f"output_folder={self.store / 'installed-disks'}",
            "memory_feed": f"output_folder={Path.cwd() / 'installed-builds'}",
            "nightly_review": "state_directory=/installed/nightly",
            "production": "\n".join(f"{key}={value}" for key, value in mac_paths.items()),
            "scratch": f"holds_file={root / 'installed-holds.json'}",
            "showrunners": "# range: 0..\nrepeat_minutes=33",
            "statusline": "\n".join(f"{key}={value}" for key, value in raw_rebuilds.items()),
        }.items():
            _ = (installed / f"{stem}.conf").write_text(content + "\n")
        links = installed / "review_links.conf"
        _ = links.write_text(links.read_text() + f"\n# whole: 1..65535\nserver_port.{host}=4321\n")
        staff = root / "staff-override.conf"
        _ = staff.write_text("fixture=installed-staff\n")
        models = root / "models-override.conf"
        _ = models.write_text("[fixture]\nmodel=installed-model\n")
        zone = root / "zone-override.conf"
        _ = zone.write_text("zone=Etc/UTC\n")
        expected: dict[str, object] = {
            "accounts": 32, "aws_page": str(Path.cwd() / "installed-aws"), "aws_target": [12, True],
            "build_admission": "installed-ci", "build_folders": {"roots.session": str(root / "installed-sessions")},
            "build_steps": "installed step",
            "change_review": str(self.store), "cloud_gate": ["us-east-1"], "codex_daemon": 2.5, "console": "installed-host",
            "machines": sorted(names),
            "memory_feed": str(Path.cwd() / "installed-builds"), "models": models.read_text(),
            "nightly_review": "/installed/nightly", "production": mac_paths, "review_links": ["override-host", 4321, "review-attachments"],
            "role": "codex1", "scratch": [True, str(root / "installed-holds.json")],
            "showrunners": 33.0, "staff": "installed-staff", "statusline": raw_rebuilds, "user_zone": "Etc/UTC",
        }
        self.assertIn("status.accounts.age_limit_seconds=49", candidate.read_text())
        server_inputs: list[object] = []
        child_inputs: list[object] = []
        child_pages: list[bytes] = []
        disks_paths: list[bytes] = []
        def visit(browser: Browser, path: str, _viewport: Viewport, _limit: float,
                  key: str, _direct: str) -> PageResult:
            if key == "first":
                self.assertIn(b"Page ready", self.fetch(browser, first_page()))
                shell = self.fetch(browser, path)
                setting = re.search(rb'data-url="/fixture/first.html\?([^"]+)"', shell)
                assert setting is not None, shell
                server_inputs.append(cast(object, json.loads(urlsafe_b64decode(setting[1]))))
            elif key == "second":
                page = self.fetch(browser, accounts_page.PAGE_PATH)
                child_pages.append(page)
                child_inputs.append(cast(object, json.loads(page.split(b"<!--settings=")[1].removesuffix(b"-->"))))
            elif key == "disks":
                shell = self.fetch(browser, path)
                setting = re.search(rb'data-url="(/[^"/]+-disks)/disks.html"', shell)
                assert setting is not None, shell
                disks_paths.append(setting[1])
                report = message_object(self.fetch(browser, setting[1].decode() + "/disks.json"))
                self.assertEqual(report["generation"], 17)
            return PageResult(0, ())

        with (patch.dict(os.environ, {"REVIEW_LINKS_HOST": "override-host", "STAFF_CONFIG": str(staff), "SCRATCH_HOLDS": "",
                                     "MODELS_CONFIG_FILE": str(models), "USER_ZONE_CONF": str(zone), "USER_ZONE": ""}),
              patch.object(console_settings, "installed_checkout", return_value=checkout),
              self.http_visits(visit), redirect_stdout(StringIO()) as output):
            self.assertEqual(console_check.run_check(self.candidate, base=base), "", output.getvalue())
        self.assertCountEqual(disks_paths, [b"/base-disks", b"/candidate-disks"], output.getvalue())
        self.assertEqual(server_inputs, [expected, expected], output.getvalue())
        self.assertEqual(child_inputs, [expected, expected])
        self.assertCountEqual([re.findall(rb'ageLimit&quot;: (\d+)', page) for page in child_pages],
                              [[b"47"], [b"49"]])

    def test_accounts_child_reads_the_candidate_age_limit(self) -> None:
        settings = self.configure_accounts_child()
        _ = settings.write_text(settings.read_text().replace("status.accounts.age_limit_seconds=45",
                                                            "status.accounts.age_limit_seconds=49"))
        responses: list[bytes] = []
        with self.http_visits(self.read_accounts_on_visit(responses)), redirect_stdout(StringIO()) as output:
            self.assertEqual(console_check.run_check(self.candidate), "", output.getvalue())
        self.assertEqual(len(responses), 1)
        self.assertIn(b"&quot;ageLimit&quot;: 49.0", responses[0])

    def test_invalid_candidate_accounts_age_limit_is_a_page_fault(self) -> None:
        self.enterContext(self.http_visits())
        settings = self.configure_accounts_child()
        _ = settings.write_text(settings.read_text().replace("status.accounts.age_limit_seconds=45",
                                                            "status.accounts.age_limit_seconds=invalid"))
        with redirect_stdout(StringIO()):
            refusal = console_check.run_check(self.candidate)
        self.assertIn("RED: console check. Second:", refusal)
        self.assertIn("status.accounts.age_limit_seconds='invalid'", refusal)

    def test_exported_base_accounts_child_uses_its_page_settings_and_installed_recovery(self) -> None:
        candidate = self.configure_accounts_child()
        base = self.legacy_console_base()
        base_settings = base / "config/console.conf"
        _ = base_settings.write_text(base_settings.read_text().replace("status.accounts.age_limit_seconds=45",
                                                                     "status.accounts.age_limit_seconds=47"))
        base_renderer = base / "scripts/review_links/accounts_page.py"
        contents = base_renderer.read_text().replace("sys.stdout.buffer.write(page())",
                                                     "sys.stdout.buffer.write(page() + b'<!-- Base Accounts child -->')")
        self.replace_script(base_renderer, contents)
        self.commit_console_base(base)
        _ = candidate.write_text(candidate.read_text().replace("status.accounts.age_limit_seconds=45",
                                                               "status.accounts.age_limit_seconds=49"))
        installed = Path(os.environ["CONF_DIR"])
        self.git(installed, "init", "-b", "main")
        self.commit_fixture_files(installed, "Set the installed machine aliases")
        machines = installed / "machines.conf"
        _ = machines.write_text("\n".join(line for line in machines.read_text().splitlines()
                                            if not line.startswith("aliases=")))
        responses: list[bytes] = []
        with self.http_visits(self.read_accounts_on_visit(responses)), redirect_stdout(StringIO()) as output:
            self.assertEqual(console_check.compare_to_main(base, "HEAD", self.candidate), "", output.getvalue())
        self.assertIn("base console Second: ok", output.getvalue())
        self.assertIn("candidate console Second: ok", output.getvalue())
        self.assertEqual(len(responses), 2)
        base_page = next(page for page in responses if b"<!-- Base Accounts child -->" in page)
        candidate_page = next(page for page in responses if page is not base_page)
        self.assertIn(b"&quot;ageLimit&quot;: 47.0", base_page)
        self.assertIn(b"&quot;ageLimit&quot;: 49.0", candidate_page)

    def test_different_accounts_child_settings_faults_hold_promotion(self) -> None:
        self.enterContext(self.http_visits())
        candidate = self.configure_accounts_child()
        base = self.candidate.with_name("base")
        self.copy_candidate(base)
        for path, value in ((base / "config/console.conf", "base-invalid"), (candidate, "candidate-invalid")):
            _ = path.write_text(path.read_text().replace("status.accounts.age_limit_seconds=45",
                                                       f"status.accounts.age_limit_seconds={value}"))
        with redirect_stdout(StringIO()) as output:
            refusal = console_check.run_check(self.candidate, base=base)
        self.assertIn("RED: console check. Second:", refusal)
        self.assertIn("<console-config>/console.conf: status.accounts.age_limit_seconds='candidate-invalid'", refusal)
        self.assertIn("<console-config>/console.conf: status.accounts.age_limit_seconds='base-invalid'", output.getvalue())

    def test_base_adapter_imports_the_base_tree(self) -> None:
        self.enterContext(self.http_visits())
        base = self.candidate.with_name("base")
        self.copy_candidate(base)
        resolver = base / "scripts/review_links/fixture_base_resolver.py"
        _ = resolver.write_text(
            "from scripts.production import change_review, change_routing\n"
            + "def page_url():\n"
            + "    with change_review.review_delivery_lock(change_routing.review_root()):\n"
            + '        return "/fixture/first.html"\n')
        lock = self.store / ".review-link-delivery.lock"
        _ = lock.write_text("Live lock fixture.\n")
        self.page_function("page.first.url", "python:scripts.review_links.fixture_base_resolver:page_url", base)
        base_adapter = base / console_check.SERVER_MODULE
        is_file = Path.is_file

        def legacy_tree_has_no_adapter(path: Path) -> bool:
            return path != base_adapter and is_file(path)

        with patch.object(Path, "is_file", legacy_tree_has_no_adapter), redirect_stdout(StringIO()) as output:
            self.assertEqual(console_check.run_check(self.candidate, base=base), "", output.getvalue())
        self.assertIn("base console First: ok", output.getvalue())
        self.assertEqual(lock.read_text(), "Live lock fixture.\n")

    def check_render_attempts(self, candidate_results: tuple[PageResult, ...],
                              base_results: tuple[PageResult, ...] = (), *, name: str = "First") -> str:
        clean = PageResult(1, ())
        attempts = {"base": iter(base_results or (clean,) * len(candidate_results)), "candidate": iter(candidate_results)}
        visited: list[str] = []

        def visit(browser: Browser, _path: str, _viewport: Viewport, _limit: float,
                  page_key: str, _direct: str) -> PageResult:
            visited.append(page_key)
            return next(attempts[browser.origin]) if page_key == "first" else clean

        sessions: list[console_check.ConsoleSession] = []
        for label in attempts:
            browser = cast(Browser, Mock(spec=Browser, origin=label))
            log = self.candidate / f"{label}.log"
            log.touch()
            pages: dict[str, console_check.ReadyPage | console_check.PageSetupFault] = {
                key: {"identity": key, "label": name if key == "first" else key.title(), "path": f"/fixture/{key}.html"}
                for key in ("first", "second", "rebuilds")
            }
            sessions.append(console_check.ConsoleSession(self.candidate, label, pages, browser, log, log))

        def prepare(session: console_check.ConsoleSession, page: console_check.PageVisit,
                    _attempt: int) -> console_check.VisitSetup:
            return session.pages[page.page_key]

        def configure_visit(session: console_check.ConsoleSession) -> None:
            def render(path: str, viewport: Viewport, limit: float, page_key: str, direct: str = "") -> PageResult:
                return visit(session.browser, path, viewport, limit, page_key, direct)

            session.browser.visit = render

        for session in sessions:
            configure_visit(session)
        with patch.object(console_check.ConsoleSession, "prepare", prepare):
            failures = console_check.check_sessions(sessions, [], 12)
        self.assertEqual(visited.count("first"), 2 * len(candidate_results))
        self.assertEqual(visited.count("second"), 2)
        self.assertEqual(visited.count("rebuilds"), 2)
        return "RED: console check. " + " | ".join(failures) if failures else ""

    def test_retry_resolves_only_the_affected_page_and_observes_live_setup(self) -> None:
        helper = "scripts.production.test_console_fixtures"
        self.page_function("page.second.url", f"python:{helper}:page_with_live_setup", self.candidate)
        base = self.candidate.with_name("base")
        self.copy_candidate(base)
        flag = self.candidate.parent / "setup-unavailable"
        flag.touch()
        # The earlier page fails setup if any retry resolves it again.
        for tree in (base, self.candidate):
            fixtures = tree / "scripts/production/test_console_fixtures.py"
            text = fixtures.read_text()
            marker = '    return "/fixture/first.html"'
            self.replace_script(fixtures, text.replace(marker,
                '    assert Path(os.environ["CONSOLE_TEST_SETUP_FLAG"]).exists()\n' + marker, 1))
        visits: list[str] = []

        def visit(browser: Browser, path: str, _viewport: Viewport, _limit: float,
                  key: str, _direct: str) -> PageResult:
            if key == "first":
                flag.unlink(missing_ok=True)
            if key == "second":
                shell = self.fetch(browser, path)
                self.assertIn(b'data-url="/fixture/first.html"', shell)
                self.assertIn(b'data-url="/fixture/second.html"', shell)
                visits.append(key)
            return PageResult(0, ())

        with (patch.dict(os.environ, {"CONSOLE_TEST_SETUP_FLAG": str(flag)}), self.http_visits(visit),
              redirect_stdout(StringIO()) as output):
            refusal = console_check.run_check(self.candidate, base=base)
        self.assertEqual(refusal, "RED: console check. Second: The fixture input is unavailable. | "
                         + "The candidate input failed on retry.", output.getvalue())
        self.assertIn("base console Second: red", output.getvalue())
        self.assertEqual(output.getvalue().count("base console Second: ok"), 2)
        self.assertEqual(output.getvalue().count("candidate console Second: red"), 3)
        self.assertEqual(visits, ["second", "second"])

    def test_late_review_render_cannot_replace_the_retry_snapshot(self) -> None:
        _ = self.configure_accounts_child()
        snapshot = review_page.build_snapshot(self.store)
        started = threading.Event()
        finish = threading.Event()
        versions = iter(("first", "retry"))

        def build(_root: Path) -> review_page.ReviewSnapshot:
            version = next(versions)
            if version == "first":
                started.set()
                self.assertTrue(finish.wait(5))
            return replace(snapshot, version=version)

        def request(path: str, attempt: str, port: int, expected: int = 200) -> bytes:
            connection = HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                connection.request("GET", path, headers={"X-Console-Attempt": attempt})
                response = connection.getresponse()
                content = response.read()
                self.assertEqual(response.status, expected, content)
                return content
            finally:
                connection.close()

        page = review_page.PAGE_PATH
        destination = console.AvailablePage(page)
        registry = (console.ConsolePage("review", "Review", "inbox", "Review", destination),)
        resolution = console_server.ResolvedPage(destination, {})
        with (patch.object(console, "render", console.render), patch.object(review_page, "build_snapshot", build),
              console_server.comparison_server(self.store, registry, console.UnavailablePage(),
                                               {"review": resolution}, lambda _key, _attempt: resolution) as server,
              ThreadPoolExecutor(max_workers=2) as workers):
            serving = workers.submit(server.serve_forever)
            try:
                port = server.server_port
                first_attempt, retry_attempt = "1" * 32, "2" * 32
                late = workers.submit(request, page, first_attempt, port)
                self.assertTrue(started.wait(5))
                route = "/__console_check/resolve?page=review"
                _ = request(route, "invalid", port, 400)
                _ = request(route, retry_attempt, port)
                _ = request(page, retry_attempt, port)
                finish.set()
                _ = late.result(timeout=5)
                for attempt, version in ((first_attempt, "first"), (retry_attempt, "retry")):
                    result = message_object(request(page + "?version=1", attempt, port))
                    self.assertEqual(result["version"], version)
            finally:
                finish.set()
                server.shutdown()
                serving.result(timeout=5)

    def test_candidate_that_renders_under_the_limit_on_retry_passes(self) -> None:
        timeout = PageResult(12.1, (PageFault(FaultKind.RENDER_LIMIT, "Render exceeded 12 s."),), VisitCompletion.INCOMPLETE)
        self.assertEqual(self.check_render_attempts((timeout, PageResult(1, ()), timeout)), "")

    @unittest.skipUnless(browser_available(), "Headless Chrome is unavailable.")
    def test_chrome_counts_one_accounts_504_timeout_and_recovers(self) -> None:
        _ = self.configure_accounts_child()
        base = self.candidate.with_name("base")
        self.copy_candidate(base)
        flag = self.candidate.parent / "accounts-timeout"
        flag.touch()
        for tree in (base, self.candidate):
            renderer = tree / "scripts/review_links/accounts_page.py"
            content = renderer.read_text().replace("    _ = sys.stdout.buffer.write(page())",
                                                   '    _ = sys.stdout.buffer.write(b"<h1>Accounts ready</h1>")')
            if tree == self.candidate:
                marker = "        done = subprocess.run(CHILD, capture_output=True, timeout=seconds, check=False)"
                self.assertIn(marker, content)
                content = content.replace(marker, f"        if Path({str(flag)!r}).exists():\n"
                    + "            raise subprocess.TimeoutExpired(CHILD, seconds)\n" + marker)
            self.replace_script(renderer, content)
        timeouts: list[PageResult] = []
        visit = Browser.visit

        def record_timeout(browser: Browser, path: str, viewport: Viewport, limit: float,
                           key: str, direct: str) -> PageResult:
            result = visit(browser, path, viewport, limit, key, direct)
            if key == "second" and browser.server_timed_out:
                timeouts.append(result)
                flag.unlink(missing_ok=True)
            return result

        with patch.object(Browser, "visit", record_timeout), redirect_stdout(StringIO()) as output:
            self.assertEqual(console_check.run_check(self.candidate, base=base), "", output.getvalue())
        self.assertEqual(len(timeouts), 1)
        self.assertIs(timeouts[0].completion, VisitCompletion.INCOMPLETE)
        self.assertEqual([(fault.kind, fault.detail) for fault in timeouts[0].faults],
                         [(FaultKind.RENDER_LIMIT, "Render timed out at /console/accounts.html.")])
        self.assertEqual(output.getvalue().count("candidate console Second: ok"), 2)

    def test_accounts_render_deadline_terminates_the_child(self) -> None:
        settings = self.candidate / "config/review_links.conf"
        contents, count = re.subn(r"(?m)^accounts_page_seconds=.*$", "accounts_page_seconds=1", settings.read_text())
        self.assertEqual(count, 1)
        _ = settings.write_text(contents)
        pid = self.candidate / "child.pid"
        command = (sys.executable, "-c",
                   f"import os, signal; open({str(pid)!r}, 'w').write(str(os.getpid())); signal.pause()")
        with patch.object(accounts_page, "CHILD", command), self.assertRaises(OSError) as failure:
            _ = accounts_page.render_with_facts()
        self.assertIsInstance(failure.exception.__cause__, subprocess.TimeoutExpired)
        child = int(pid.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)

    def test_incomplete_browser_setup_retries_the_render_pair(self) -> None:
        base = self.candidate.with_name("base")
        self.copy_candidate(base)
        attempts: dict[str, int] = {}

        def visit(browser: Browser, _path: str, _viewport: Viewport, _limit: float,
                  key: str, _direct: str) -> PageResult:
            if key == "first":
                count = attempts.get(browser.origin, 0) + 1
                attempts[browser.origin] = count
                if count == 1:
                    raise TimeoutError("Fixture browser setup did not complete.")
            return PageResult(0, ())

        with self.http_visits(visit), redirect_stdout(StringIO()) as output:
            self.assertEqual(console_check.run_check(self.candidate, base=base), "", output.getvalue())
        self.assertCountEqual(attempts.values(), [3, 3])
        self.assertEqual(output.getvalue().count("Browser setup did not complete."), 2)

    def test_accounts_timeout_preserves_independent_http_and_script_faults(self) -> None:
        with subprocess.Popen(["true"], stdin=subprocess.PIPE, stdout=subprocess.PIPE) as child:
            browser = Browser(child, "http://127.0.0.1:1")
            browser.session = "fixture"
            for path, headers in ((accounts_page.PAGE_PATH, {"X-Console-Render-Timeout": "accounts"}),
                                  ("/fixture/upstream", {})):
                browser.event({"sessionId": browser.session, "method": "Network.responseReceived",
                               "params": {"response": {"url": browser.origin + path, "status": 504,
                                                        "headers": headers}}})
            browser.event({"sessionId": browser.session, "method": "Runtime.exceptionThrown",
                           "params": {"exceptionDetails": {"text": "Fixture script failed"}}})
        self.assertTrue(browser.server_timed_out)
        self.assertEqual([fault.kind for fault in browser.faults],
                         [FaultKind.RENDER_LIMIT, FaultKind.HTTP_SERVER, FaultKind.SCRIPT])
        self.assertIn("/fixture/upstream", browser.faults[1].detail)
        self.assertIn("Fixture script failed", browser.faults[2].detail)

    def test_candidate_that_crosses_the_limit_three_times_refuses_promotion(self) -> None:
        timeout = PageResult(12.1, (PageFault(FaultKind.RENDER_LIMIT, "Render exceeded 12 s."),), VisitCompletion.INCOMPLETE)
        refusal = self.check_render_attempts((timeout, timeout, timeout))
        self.assertIn("RED: console check. First: Render exceeded 12 s.", refusal)
        self.assertIn("did not complete a render pair", refusal)

    def test_http_fault_on_both_render_retries_refuses_promotion(self) -> None:
        timeout = PageResult(12.1, (PageFault(FaultKind.RENDER_LIMIT, "Render exceeded 12 s."),), VisitCompletion.INCOMPLETE)
        unavailable = PageResult(1, (PageFault(FaultKind.HTTP_SERVER, "HTTP 503 /fixture/first.html"),))
        self.assertEqual(self.check_render_attempts((timeout, unavailable, unavailable)),
                         "RED: console check. First: HTTP 503 /fixture/first.html")

    def test_script_fault_on_second_render_retry_refuses_promotion(self) -> None:
        timeout = PageResult(12.1, (PageFault(FaultKind.RENDER_LIMIT, "Render exceeded 12 s."),), VisitCompletion.INCOMPLETE)
        broken = PageResult(1, (PageFault(FaultKind.SCRIPT, "Fixture script failed."),))
        self.assertEqual(self.check_render_attempts((timeout, PageResult(1, ()), broken)),
                         "RED: console check. First: Fixture script failed.")

    def test_third_visit_reports_both_render_timeout_and_script_fault(self) -> None:
        timeout = PageResult(12.1, (PageFault(FaultKind.RENDER_LIMIT, "Render exceeded 12 s."),), VisitCompletion.INCOMPLETE)
        broken = PageResult(12.1, (*timeout.faults, PageFault(FaultKind.SCRIPT, "Fixture script failed.")),
                            VisitCompletion.INCOMPLETE)
        refusal = self.check_render_attempts((timeout, timeout, broken))
        self.assertIn("Fixture script failed.", refusal)
        self.assertIn("Render exceeded 12 s.", refusal)

    def test_read_only_accounts_and_herdr_do_not_fetch_or_recover(self) -> None:
        from scripts.accounts import accounts
        from scripts.herdr import pane_launch

        _ = self.configure_accounts_child()
        panes = pane_launch.record_directory()
        panes.mkdir()
        path = panes / "fixture.json"
        _ = path.write_text('{"fixture":{}}')
        recovery = panes / ".producer-publication"
        _ = recovery.write_text("Unfinished publication")
        with (patch.dict(os.environ, {"REVIEW_LINKS_READ_ONLY": "1"}),
              patch.object(accounts, "_start_session_counts_fetch") as fetch):
            _ = accounts.read_status(source=accounts.StoredUsageOnly())
            self.assertEqual(pane_launch.read_entries(path), {"fixture": {}})
            fetch.assert_not_called()
        self.assertFalse((panes / ".publication.lock").exists())
        self.assertEqual(recovery.read_text(), "Unfinished publication")
        self.assertFalse((Path.home() / ".local/state/accounts").exists())

    def test_guard_allows_the_darwin_ps_argument_order(self) -> None:
        result = subprocess.run([sys.executable, "-c",
            "import os, subprocess; from pathlib import Path; "
            + "from scripts.production.console_write_scope import install_barrier; "
            + "install_barrier(Path.cwd(), 'test'); "
            + "subprocess.run(['ps', '-p', str(os.getpid()), '-o', 'args='], check=True, capture_output=True)"],
            cwd=self.candidate, capture_output=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.candidate / "console-barrier.log").read_text(), "")

    def test_missing_browser_names_the_configuration_failure(self) -> None:
        with patch.object(shutil, "which", return_value=None), patch.object(os, "access", return_value=False):
            self.assertIn("No supported browser found", console_check.run_check(self.candidate))

    def test_a_base_fault_from_one_attempt_does_not_clear_the_candidate_fault(self) -> None:
        clean = PageResult(1, ())
        failed = PageResult(1, (PageFault(FaultKind.SCRIPT, "Fixture script failed."),))
        refusal = self.check_render_attempts((failed, clean, clean), (failed, clean, clean))
        self.assertEqual(refusal, "RED: console check. First: Fixture script failed.")

    def test_matching_accounts_timeouts_do_not_complete_the_check(self) -> None:
        timeout = PageResult(12, (PageFault(FaultKind.RENDER_LIMIT, "Render exceeded 12 s."),),
                             VisitCompletion.INCOMPLETE)
        with redirect_stdout(StringIO()) as output:
            refusal = self.check_render_attempts((timeout,) * 3, (timeout,) * 3, name="Accounts")
        self.assertIn("Accounts: base and candidate did not complete a render pair. Render exceeded 12 s.", refusal)
        self.assertNotIn("live console faults (not held) Accounts", output.getvalue())
        self.assertEqual(output.getvalue().count("Accounts: red"), 6)

    def test_broken_candidate_registry_fails_while_the_base_renders(self) -> None:
        self.enterContext(self.http_visits())
        base = self.candidate.with_name("base")
        self.copy_candidate(base)
        settings = self.candidate / "config/console.conf"
        _ = settings.write_text(settings.read_text().replace("page.first.icon=inbox", "page.first.icon=invalid"))
        with redirect_stdout(StringIO()) as output:
            refusal = console_check.run_check(self.candidate, base=base)
        self.assertIn("candidate console server startup", refusal)
        self.assertIn("provide a label, title, and supported Lucide icon", refusal)
        self.assertIn("base console First: ok", output.getvalue())

    def test_buffered_events_without_a_reply_end_with_a_render_timeout(self) -> None:
        command = [sys.executable, "-c", "import os, sys; os.close(1); sys.stdin.buffer.read()"]
        with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE) as child:
            browser = Browser(child, "http://127.0.0.1:1")
            browser.input.buffer = b'{"method":"Fixture.event"}\0' * 3
            with patch.object(time, "monotonic", side_effect=(0.0, 0.15, 0.36, 0.36)):
                result = browser.navigate("/", 0.1, "accounts")
        self.assertEqual([fault.kind for fault in result.faults], [FaultKind.RENDER_LIMIT])
        self.assertLess(result.seconds, 0.4)

    def test_generation_failure_is_a_page_fault_on_either_tree(self) -> None:
        self.enterContext(self.http_visits())
        self.configure_generated_folders()
        base = self.candidate.with_name("base")
        self.copy_candidate(base)
        broken = "python:scripts.production.test_console_fixtures:broken_disks_generation"
        self.page_function("page.first.url", broken, base)
        self.page_function("page.second.url", broken, self.candidate)
        with redirect_stdout(StringIO()) as output:
            refusal = console_check.run_check(self.candidate, base=base)
        self.assertIn("live console faults (not held) First: Fixture Disks generation failed.", output.getvalue())
        self.assertEqual(refusal, "RED: console check. Second: Fixture Disks generation failed.")

    def test_read_only_server_refuses_console_writes(self) -> None:
        disks = self.store / "disks"
        disks.mkdir()
        _ = (self.candidate / "config/disks.conf").write_text(f"output_folder={disks}\n")
        _ = (self.candidate / "config/build_folders.conf").write_text("# No slots in this fixture.\n")
        reading = disks / "disks.json"
        published = '{"generation": 17, "measured_at": 1, "render_seconds": 0, "keep_bytes": 0, "drives": []}'
        _ = reading.write_text(published)
        mode = serve.ReadOnlyConsole((), console.UnavailablePage())
        with serve.ReviewLinksServer(("127.0.0.1", 0), self.store, mode=mode) as server:
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                headers = {"Content-Type": "application/x-www-form-urlencoded"}
                for path, body in (("/console/news", "open=first"), ("/disks/measure", "force=1")):
                    with self.subTest(path=path):
                        connection.request("POST", path, body, headers)
                        reply = connection.getresponse()
                        self.assertEqual(reply.status, 405)
                        self.assertIn(b"This console is read-only", reply.read())
                connection.request("POST", "/disks/measure", "", headers)
                reply = connection.getresponse()
                self.assertEqual(reply.status, 200)
                payload = message_object(reply.read())
                self.assertEqual((payload["state"], payload["target"]), ("current", 17))
                self.assertEqual(reading.read_text(), published)
            finally:
                connection.close()
                server.shutdown()
                thread.join(5)
