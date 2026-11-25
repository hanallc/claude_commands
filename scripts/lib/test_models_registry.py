"""One isolated, complete agent registry for Python and shell tests."""
from __future__ import annotations

import argparse
from concurrent.futures import Future, ThreadPoolExecutor
import fcntl
import io
import json
import os
import pwd
import shutil
import subprocess
import tempfile
from threading import Event, get_ident
import unittest
from contextlib import contextmanager, redirect_stderr
from collections.abc import Generator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO, cast, final, override
from unittest.mock import patch

from scripts.lib import land_main, live_settings, main_push_lock, models_registry, names, registry_rows, waiting_rows
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.checkout_test_support import write_python_tool
from scripts.lib.rerun_test_support import ScriptRepositoryTestCase
from scripts.lib.test_config_form import check_blank_lines
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

FIXTURE = Path(__file__).with_name("test_models_registry.conf")


@final
@dataclass(frozen=True)
class ExactRows:
    """A section contains only these rows, without inheriting the baseline's rows."""

    rows: Mapping[str, str | None]


RegistryOverrides = Mapping[str, Mapping[str, str | None] | ExactRows]
NO_OVERRIDES: RegistryOverrides = {}


@final
class RegistryReadProbe:
    """Count registry opens in every Python child of a fixture launch."""

    def __init__(self, root: Path, registry: Path) -> None:
        self.directory = root / "registry-read-probe"
        self.directory.mkdir(exist_ok=True)
        self.log = self.directory / "reads"
        self.registry = registry.resolve()
        self.local_reads = 0
        _ = (self.directory / "sitecustomize.py").write_text('''import os, sys
target = os.environ.get("TEST_REGISTRY_PATH", "")
log = os.environ.get("TEST_REGISTRY_READS", "")
def registry_open(event, arguments):
    if event != "open" or not target or not log:
        return
    path, mode, flags = arguments
    if not isinstance(path, (str, bytes)) or os.fsdecode(path) != target:
        return
    if (isinstance(mode, str) and "r" in mode) or (mode is None and flags & os.O_ACCMODE == os.O_RDONLY):
        descriptor = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(descriptor, (str(os.getpid()) + " " + sys.argv[0] + "\\n").encode())
        finally:
            os.close(descriptor)
sys.addaudithook(registry_open)
''')
        self.clear()

    def environment(self, environment: Mapping[str, str]) -> dict[str, str]:
        return {**environment, "TEST_REGISTRY_PATH": str(self.registry), "TEST_REGISTRY_READS": str(self.log),
                "PYTHONPATH": str(self.directory) + os.pathsep + environment.get("PYTHONPATH", "")}

    @contextmanager
    def active(self, environment: Mapping[str, str]) -> Generator[None]:
        from scripts.lib import models_registry

        self.local_reads = 0
        with patch.dict(os.environ, self.environment(environment)), \
                patch.object(models_registry, "registry_text", wraps=models_registry.registry_text) as reads:
            try:
                yield
            finally:
                self.local_reads = reads.call_count

    def total(self) -> int:
        return self.local_reads + len(self.reads())

    def reads(self) -> list[str]:
        return self.log.read_text().splitlines()

    def clear(self) -> None:
        self.local_reads = 0
        _ = self.log.write_text("")


def registry_text(overrides: RegistryOverrides = NO_OVERRIDES, *,
                  replace_sections: Sequence[str] = (), base_text: str = "",
                  bare_rows: Sequence[str] = ()) -> str:
    """Merge row overrides; ExactRows replaces one section, including assignments."""
    sections: dict[str, dict[str, str | None]] = {}
    section = ""
    for line in (base_text or FIXTURE.read_text(encoding="utf-8")).splitlines():
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            _ = sections.setdefault(section, {})
        elif "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            sections.setdefault(section, {})[key.strip()] = value
        elif line.strip() and not line.lstrip().startswith("#"):
            sections.setdefault(section, {})[line.strip()] = None
    replaced = set(replace_sections)
    replaced.update(section for section, rows in overrides.items() if isinstance(rows, ExactRows))
    for section in replaced:
        sections[section] = {}
    for section, rows in overrides.items():
        target = sections.setdefault(section, {})
        for key, value in (rows.rows if isinstance(rows, ExactRows) else rows).items():
            if value is None:
                _ = target.pop(key, None)
            else:
                target[key] = value
    for address in bare_rows:
        section, key = address.rsplit("/", 1)
        sections.setdefault(section, {})[key] = None
    return "\n\n".join(f"[{section}]\n" + "\n".join(key if value is None else f"{key}={value}"
                                                   for key, value in rows.items())
                          for section, rows in sections.items() if rows) + "\n"


def write_registry(path: Path, overrides: RegistryOverrides = NO_OVERRIDES, *,
                   replace_sections: Sequence[str] = (), update: bool = False, base_text: str = "",
                   bare_rows: Sequence[str] = ()) -> Path:
    """Write a fixture registry, refusing the user's real models.conf."""
    real_config = (Path(pwd.getpwuid(os.getuid()).pw_dir) / ".claude/config").resolve()
    if path.resolve() == real_config / "models.conf":
        raise AssertionError(f"test registry must be temporary: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    base_text = path.read_text(encoding="utf-8") if update and path.exists() else base_text
    _ = path.write_text(registry_text(overrides, replace_sections=replace_sections, base_text=base_text,
                                    bare_rows=bare_rows), encoding="utf-8")
    return path


def claude_catalog_environment(directory: Path, targets: Mapping[str, str]) -> dict[str, str]:
    """Create a catalog and CLI fixture for a Claude account."""
    catalog = directory / "cache/model-catalog/fixture-cc.json"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    _ = catalog.write_text(json.dumps({
        "fetchedAt": 2000,
        "catalog": {"config": {
            "provider_alias_targets": {word: {"default": target} for word, target in targets.items()},
            "models": [{"id": target, "min_claude_code_version": "2.1.319"} for target in targets.values()],
        }},
    }))
    binaries = directory / "bin"
    binaries.mkdir(exist_ok=True)
    claude = binaries / "claude"
    _ = claude.write_text("#!/bin/sh\nprintf '%s\\n' '2.1.319 (Claude Code)'\n")
    claude.chmod(0o755)
    return {"CLAUDE_CONFIG_DIR": str(directory), "PATH": str(binaries) + os.pathsep + os.environ.get("PATH", "")}


class RegistryPublicationTests(ScriptRepositoryTestCase):
    def _save_during_registry_move(self, *values: str) -> tuple[str, list[str]]:
        registry = self.write(self.repo / "config/models.conf", "[fixture]\nmodel=old\n")
        colors = self.write(self.repo / "config/statusline.conf", "account.claude1=brown\n")
        self.seed()
        base = self.git("rev-parse", "HEAD").stdout.strip()
        _ = self.write(colors, "account.claude1=#445566\n")
        _ = self.git("commit", "-qam", "incoming color")
        _ = self.git("push", "-q", "origin", "main")
        _ = self.git("reset", "--hard", base)
        _ = self.write(colors, "account.claude1=#112233\n")

        environment = {**self.environment, "CONF_DIR": str(colors.parent),
                       "XDG_STATE_HOME": str(self.root / "state"), "MODELS_CONFIG_PUBLISH": "1",
                       "TMPDIR": str(self.root)}
        caller = get_ident()
        attempted, blocked = Event(), Event()
        flock, give = fcntl.flock, waiting_rows.give
        saves: list[Future[list[int]]] = []

        def observe_lock(descriptor: TextIO, operation: int) -> None:
            if get_ident() != caller and operation == fcntl.LOCK_EX:
                try:
                    flock(descriptor, operation | fcntl.LOCK_NB)
                except BlockingIOError:
                    blocked.set()
                else:
                    attempted.set()
                    return
                attempted.set()
            flock(descriptor, operation)

        def save() -> list[int]:
            return [live_settings.main(["statusline", "account.claude1", value]) for value in values]

        with patch.dict(os.environ, environment, clear=True), \
                patch.object(models_registry, "DEFAULT_PATH", registry), \
                patch.object(fcntl, "flock", observe_lock), ThreadPoolExecutor(max_workers=1) as executor:
            def start_save(checkout: Path, rows: waiting_rows.WaitingRows,
                           environment: dict[str, str] | None = None) -> None:
                if environment is None:
                    saves.append(executor.submit(save))
                    self.assertTrue(attempted.wait(TEST_HANG_GUARD_SECS))
                    self.assertTrue(blocked.is_set(), "The Save acquired the lock during the move")
                give(checkout, rows, environment)

            before = registry.read_text()
            with patch.object(waiting_rows, "give", start_save):
                _ = models_registry.store_registry(registry, before, before, "fixture")
            self.assertEqual(len(saves), 1)
            self.assertEqual(saves[0].result(timeout=TEST_HANG_GUARD_SECS), [0] * len(values))
            with waiting_rows.locked_journal(waiting_rows.journal_path()) as journal:
                queued = [row.value for row in journal.changes]
                queued.extend(save.value for keys in journal.accepted.values() for save in keys.values()
                              if isinstance(save, waiting_rows.AcceptedSave))
                return colors.read_text(), queued

    def test_registry_pull_and_save_share_one_lock_until_restore(self) -> None:
        text, pending = self._save_during_registry_move("#667788", "#112233")
        self.assertEqual(text, "account.claude1=#112233\n")
        self.assertEqual(pending, ["#112233"])

    def test_a_save_matching_the_registry_pull_leaves_no_receipt(self) -> None:
        text, pending = self._save_during_registry_move("#445566")
        self.assertEqual(text, "account.claude1=#445566\n")
        self.assertEqual(pending, [])

    def test_registry_publication_finishes_a_failed_write_at_the_same_tip(self) -> None:
        before = "[fixture]\nmodel=old\nsecond=old\n"
        registry = self.write(self.repo / "config/models.conf", before)
        self.seed()
        _ = self.write(registry, before.replace("second=old", "second=new"))
        _ = self.git("commit", "-qam", "incoming registry")
        target = self.git("rev-parse", "HEAD").stdout.strip()
        _ = self.git("push", "-q", "origin", "main")
        _ = self.git("reset", "--hard", "HEAD^")
        local = before.replace("model=old", "model=local")
        _ = self.write(registry, local)
        def fail_registry(file: Path, text: str, *, new_file_mode: int | None = None) -> None:
            if file == registry:
                raise OSError("fixture registry write failed")
            write_text_atomic(file, text, new_file_mode=new_file_mode)

        environment = {**self.environment, "MODELS_CONFIG_PUBLISH": "1",
                       "XDG_STATE_HOME": str(self.root / "state"), "TMPDIR": str(self.root)}
        expected = local.replace("second=old", "second=new")
        with patch.dict(os.environ, environment, clear=True), patch.object(models_registry, "DEFAULT_PATH", registry):
            with patch.object(waiting_rows, "write_text_atomic", fail_registry):
                with self.assertRaisesRegex(OSError, "fixture registry write failed"):
                    _ = models_registry.store_registry(registry, local, local, "fixture")
            self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), target)
            self.assertEqual(registry.read_text(), local)
            self.assertIn(json.dumps(expected), waiting_rows.move_marker(self.repo).read_text())
            self.assertTrue(models_registry.publish_pending(registry))
        self.assertEqual(self.git("rev-parse", "HEAD^").stdout.strip(), target)
        self.assertEqual(registry.read_text(), expected)
        self.assertEqual(self.git("show", "origin/main:config/models.conf").stdout, expected)
        self.assertFalse(waiting_rows.move_marker(self.repo).exists())

    def _publish_catalog_with_pending_family(self, *, peer_advances: bool) -> None:
        before = "[codex.families]\ncustom=gpt-6.2-sol\n[codex.agents]\ngpt-6.2-sol=low\n"
        registry = self.write(self.repo / "config/models.conf", before)
        self.seed()
        first_family = "gpt-6.1-sol" if peer_advances else "gpt-6.3-sol"
        incoming = before.replace("custom=gpt-6.2-sol", f"custom={first_family}")
        _ = self.write(registry, incoming)
        _ = self.git("commit", "-qam", "incoming family")
        _ = self.git("push", "-q", "origin", "main")
        peer_tip = ""
        if peer_advances:
            _ = self.write(registry, incoming.replace("custom=gpt-6.1-sol", "custom=gpt-6.3-sol"))
            _ = self.git("commit", "-qam", "newer peer family")
            peer_tip = self.git("rev-parse", "HEAD").stdout.strip()
        _ = self.git("reset", "--hard", "origin/main^")
        local = before.replace("custom=gpt-6.2-sol", "custom=gpt-6.1-sol")
        _ = self.write(registry, local)
        catalog = local + "gpt-6.3-sol=high\n"
        environment = {**self.environment, "MODELS_CONFIG_PUBLISH": "1",
                       "XDG_STATE_HOME": str(self.root / "state"), "TMPDIR": str(self.root)}
        land = land_main.land
        attempts: list[str] = []

        def advance_peer(repo: Path, commit: str, branch: str) -> land_main.Landed:
            attempts.append(self.git("show", f"{commit}:config/models.conf").stdout)
            if peer_tip and len(attempts) == 1:
                _ = self.git("push", "-q", "origin", f"{peer_tip}:main")
            return land(repo, commit, branch)

        spaced = catalog.replace("\n[codex.agents]", "\n\n[codex.agents]")
        expected = spaced.replace("custom=gpt-6.1-sol", "custom=gpt-6.3-sol") if peer_advances else spaced
        with patch.dict(os.environ, environment, clear=True), patch.object(models_registry, "DEFAULT_PATH", registry), \
                patch.object(land_main, "land", advance_peer):
            self.assertEqual(models_registry.store_registry(registry, local, catalog, "fixture", additive_catalog=True),
                             expected)
        self.assertEqual(self.git("show", "origin/main:config/models.conf").stdout, expected)
        self.assertEqual(attempts, [spaced, expected] if peer_advances else [spaced])

    def test_catalog_publication_keeps_an_earlier_pending_family_downgrade(self) -> None:
        self._publish_catalog_with_pending_family(peer_advances=False)

    def test_catalog_retry_keeps_the_newer_peer_family_after_the_downgrade_is_shared(self) -> None:
        self._publish_catalog_with_pending_family(peer_advances=True)

    def test_a_deferred_registry_write_waits_for_move_restore(self) -> None:
        before = "[fixture]\nmodel=old\nsecond=old\n"
        registry = self.write(self.repo / "config/models.conf", before)
        self.seed()
        base = self.git("rev-parse", "HEAD").stdout.strip()
        incoming = before.replace("second=old", "second=new")
        _ = self.write(registry, incoming)
        _ = self.git("commit", "-qam", "incoming registry")
        target = self.git("rev-parse", "HEAD").stdout.strip()
        _ = self.git("reset", "--hard", base)
        local = before.replace("model=old", "model=local")
        _ = self.write(registry, local)
        after = local.replace("model=local", "model=latest")
        environment = {**self.environment, "MODELS_CONFIG_PUBLISH": "later",
                       "XDG_STATE_HOME": str(self.root / "state"), "TMPDIR": str(self.root)}
        attempted = Event()
        blocked: list[bool] = []
        flock = fcntl.flock

        def observe_lock(lock: TextIO, operation: int) -> None:
            if Path(lock.name).name == "waiting.lock" and get_ident() != caller:
                try:
                    flock(lock, operation | fcntl.LOCK_NB)
                except BlockingIOError:
                    blocked.append(True)
                else:
                    blocked.append(False)
                attempted.set()
            flock(lock, operation)

        caller = get_ident()
        with patch.dict(os.environ, environment, clear=True), patch.object(fcntl, "flock", observe_lock), \
                ThreadPoolExecutor(max_workers=1) as executor:
            with waiting_rows.locked_journal(waiting_rows.journal_path()) as journal:
                move = waiting_rows.move_target(self.repo, target, journal, fast_forward=True)
                rows = waiting_rows.check_move(self.repo, move, journal)
                writer = executor.submit(models_registry.store_registry, registry, local, after, "deferred fixture")
                self.assertTrue(attempted.wait(TEST_HANG_GUARD_SECS))
                self.assertEqual(blocked, [True])
                self.assertEqual(registry.read_text(), local)
                waiting_rows.apply_move(self.repo, rows, journal)
            expected = after.replace("second=old", "second=new")
            self.assertEqual(writer.result(timeout=TEST_HANG_GUARD_SECS), expected)
        self.assertEqual(registry.read_text(), expected)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), target)

    def test_registry_publication_runs_the_hook_with_the_landed_commit_under_the_lock(self) -> None:
        self.seed()
        path = self.write(self.repo / "config/models.conf", "[fixture]\nmodel=old\n")
        _ = self.git("add", "config/models.conf")
        _ = self.git("commit", "-m", "seed registry")
        _ = self.git("push", "origin", "main")
        log = self.root / "hook-log"
        _ = self.write(self.repo / ".claude/config/post_push.sh",
                       f'printf "%s\\n" "$LANDED_SHA" > "{log}"\n')
        _ = self.write(self.remote / "hooks/pre-receive",
                       f'#!/bin/sh\nif flock -n "{self.repo}/.git/main-push.lock" true; then exit 93; fi\n'
                       + 'flock -n "$XDG_STATE_HOME/live-settings/waiting.lock" true\n')
        (self.remote / "hooks/pre-receive").chmod(0o755)
        tools = self.root / "lease-git"
        tools.mkdir()
        lease_log = self.root / "index-leases"
        binary = shutil.which("git")
        assert binary is not None
        write_python_tool(tools / "git", """import fcntl, os, sys
from pathlib import Path
from scripts.lib import build_folders
index = os.environ.get("GIT_INDEX_FILE", "")
if index:
    folder = Path(index).parent
    if folder.name.startswith(("models-index", "models-ff-index")):
        lock = folder.with_name(folder.name + ".lock").stat()
        inherited = []
        for entry in Path("/dev/fd").iterdir():
            try:
                if os.path.samestat(os.fstat(int(entry.name)), lock):
                    inherited.append(int(entry.name))
            except OSError:
                pass
        assert inherited, "The Git child has no folder lease"
        fcntl.flock(inherited[0], fcntl.LOCK_EX | fcntl.LOCK_NB)
        with open(""" + repr(str(lease_log)) + """, "a") as output:
            output.write(folder.name + " " + build_folders.build_folder_state(folder) + "\\n")
os.execv(""" + repr(binary) + """, [""" + repr(binary) + """, *sys.argv[1:]])
""")
        before = path.read_text()
        environment = {**self.environment, "MODELS_CONFIG_PUBLISH": "1",
                       "XDG_STATE_HOME": str(self.root / "state"),
                       "TMPDIR": str(self.root), "PATH": str(tools) + os.pathsep + self.environment.get("PATH", os.defpath)}
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(models_registry, "DEFAULT_PATH", path):
            result = models_registry.store_registry(path, before, before.replace("old", "new"), "fixture")
        landed = self.git("rev-parse", "origin/main").stdout.strip()
        self.assertEqual(result, "[fixture]\nmodel=new\n")
        self.assertEqual(log.read_text(), landed + "\n")
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), landed)
        for tool in ("models-index", "models-ff-index"):
            with self.subTest(child=tool):
                self.assertIn(tool + " held", lease_log.read_text().splitlines())
        self.assertTrue((self.repo / ".git" / main_push_lock.RECORD_NAME).is_file())


class CatalogSyncTests(unittest.TestCase):
    def test_sync_codex_catalog(self) -> None:
        script = Path(__file__).resolve().parents[1] / "agents/test_sync_codex_catalog.sh"
        result = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class RegistryRowReplayTests(unittest.TestCase):
    def test_replay_places_new_rows_and_edits_existing_rows_in_place(self) -> None:
        edit = registry_rows.RowChange("producers", "bob", "bob=claude:fable:high")
        director = registry_rows.RowChange("production.codex", "director", "director=sol:high")
        for case, change, keys, tail, added_space in (
            ("before another section", edit, "morty=claude:fable:high\ndwight=claude:fable:high\n",
             "\n# Directors\n[production.claude]\n", ""),
            ("without keys", edit, "", "# No producers\n\n[production.claude]\n", "\n"),
            ("at the end", edit, "dwight=claude:fable:high\n", "\n# End of registry\n", ""),
            ("empty production section before its comment", director, "",
             "# Set-model stores the Codex defaults for director and showrunner here.\n", "\n"),
            ("empty producers before another section", edit, "", "[assignments]\nproduction=claude\n", "\n"),
        ):
            with self.subTest(case=case):
                prefix = f"[{change.section}]\n{keys}"
                self.assertEqual(registry_rows.replay(prefix + tail, [change], False),
                                 f"{prefix}{change.replacement}\n{added_space}{tail}")

        prefix = "[producers]\nmorty=claude:fable:high\n\n[assignments]\nproduction=claude\n\n[producers]\n"
        last_key = "dwight=claude:fable:high\n"
        suffix = "\n# Directors\n[production.claude]\n"
        for case, change, expected in (
            ("new row in a repeated section", edit, f"{last_key}{edit.replacement}\n"),
            ("replace a key in the second block",
             registry_rows.RowChange("producers", "dwight", "dwight=claude:opus:high"), "dwight=claude:opus:high\n"),
            ("delete a key in the second block", registry_rows.RowChange("producers", "dwight", ""), ""),
        ):
            with self.subTest(case=case):
                self.assertEqual(registry_rows.replay(prefix + last_key + suffix, [change], False),
                                 prefix + expected + suffix)


class LaunchSettingsTests(unittest.TestCase):
    root: Path = Path()
    path: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / "models.conf"
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "HOME": str(self.root / "home"),
            "CLAUDE_CONFIG_DIR": str(self.root / "claude-config"),
            "MODELS_CONFIG_FILE": str(self.path),
            "MODELS_CONFIG_PUBLISH": "0",
            "MODELS_CONFIG_TIER_ONLY": "1",
            "CODEX_PACER_TIER_FILE": str(self.root / "absent-tier"),
        })))

    def shell_settings(self, section: str, key: str) -> tuple[str, ...]:
        result = subprocess.run([
            "bash", "-c", 'source "$1"; agents_resolve "$2.$3" || exit; ' +
            'printf "%s\\n" "$AGENT_FAMILY" "$AGENT_MODEL" "$AGENT_EFFORT" "$AGENT_SERVICE_TIER"',
            "settings-test", str(Path(__file__).resolve().parents[1] / "agents/models_config.sh"), section, key,
        ], capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        return tuple(result.stdout.splitlines())

    def test_new_producer_defaults_require_one_valid_profile(self) -> None:
        for value, valid in (("claude:sonnet:high", True), ("codex:gpt-test:high", True),
                             (None, False), ("codex:unknown:invalid", False), ("sonnet:high", False)):
            with self.subTest(value=value):
                _ = write_registry(self.path, {"producer_defaults": {"model": value}})
                if valid:
                    assert isinstance(value, str)
                    self.assertEqual(models_registry.producer_defaults().value(), value)
                else:
                    with self.assertRaisesRegex(ValueError, f"{self.path}: producer_defaults.model: [^\n]+$"):
                        _ = models_registry.producer_defaults()

    def test_launch_settings_match_shell_role_resolution_and_tier_precedence(self) -> None:
        from scripts.lib import models_registry

        # Neither resolver has a branch for one key name, so each section checks one or two keys.
        # Each case starts the shell resolver's registry reader once.
        for family in ("codex", "claude"):
            for section, keys, reference_section in (
                ("direct", ("impl", "review"), "direct"),
                ("design_roles", ("designer",), "design_roles"),
                ("producers", ("morty",), "producers"),
                ("producers", ("producer_only",), "direct"),
            ):
                for key in keys:
                    reference_key = key if reference_section == section else "impl"
                    for tier in ("", "fast", "flex", "default", "pace"):
                        with self.subTest(family=family, section=section, key=key, tier=tier):
                            _ = write_registry(self.path, {
                                "assignments": {reference_section: family},
                                f"{reference_section}.{family}": {
                                    reference_key: "gpt-test:medium" if family == "codex" else "sonnet:max"},
                                "producers": {key: f"{family}:gpt-test:medium" if family == "codex"
                                               else f"{family}:sonnet:max"},
                                **{f"{role}.options": {"codex_service_tier": "flex",
                                                      f"codex_service_tier.{role_key}": tier or None}
                                   for role, role_key in ((section, key), (reference_section, reference_key))},
                            })
                            settings = models_registry.launch_settings(section, key)
                            self.assertEqual((settings.kind, settings.model, settings.effort, settings.service_tier),
                                             self.shell_settings(reference_section, reference_key))

    def test_registry_changes_affect_the_next_launch(self) -> None:
        from scripts.lib import models_registry

        _ = write_registry(self.path, {"assignments": {"direct": "codex"}})
        first = models_registry.launch_settings("direct", "impl")
        _ = write_registry(self.path, {"assignments": {"direct": "claude"},
                                      "direct.claude": {"impl": "sonnet:max"}})
        second = models_registry.launch_settings("direct", "impl")
        self.assertNotEqual((first.kind, first.model), (second.kind, second.model))
        self.assertEqual((second.kind, second.model, second.effort, second.service_tier),
                         self.shell_settings("direct", "impl"))

        configuration = models_registry.launch_configuration(os.environ)
        _ = write_registry(self.path, {"assignments": {"direct": "codex"}})
        for inherited, expected in ((False, first), (True, second)):
            environment = configuration.environment(os.environ) if inherited else dict(os.environ)
            with self.subTest(inherited=inherited), patch.dict(os.environ, environment):
                self.assertEqual(models_registry.launch_settings("direct", "impl"), expected)

    def test_launch_settings_starts_no_python_registry_process(self) -> None:
        from scripts.lib import models_registry

        _ = write_registry(self.path, {
            "assignments": {"producers": "claude"},
            "producers.claude": {"dwight": "fable:max"},
            "producers": {"dwight": "claude:fable:max"},
        })
        text = self.path.read_text()
        _ = cast(object, self.enterContext(patch.dict(os.environ, claude_catalog_environment(
            self.root / "claude-config", {"fable": "claude-fable-fixture-current"}))))
        log = self.root / "python-processes"
        probe = self.root / "process-probe"
        probe.mkdir()
        _ = (probe / "sitecustomize.py").write_text(
            "import sys\n"
            + f"with open({str(log)!r}, 'a') as output: output.write(sys.argv[0] + '\\n')\n")
        for comment_length, section, key, expected_model in (
            (0, "direct", "impl", "gpt-test"),
            (70_000, "direct", "impl", "gpt-test"),
            (0, "producers", "dwight", "fable"),
        ):
            with self.subTest(comment_length=comment_length, section=section, key=key):
                _ = self.path.write_text("#" + "x" * comment_length + "\n" + text)
                _ = log.write_text("")
                with patch.dict(os.environ, {"PYTHONPATH": str(probe) + os.pathsep + os.environ.get("PYTHONPATH", "")}):
                    settings = models_registry.launch_settings(section, key)
                self.assertEqual(settings.model, expected_model)
                self.assertEqual(tuple(settings), self.shell_settings(section, key))
                self.assertEqual(log.read_text(), "")

    def test_invalid_launch_rows_fail_before_a_launcher_can_use_them(self) -> None:
        from scripts.lib import models_registry

        cases: tuple[tuple[str, str, RegistryOverrides], ...] = (
            ("direct", "impl", {"direct.codex": {"impl": "missing-agent:high"}}),
            ("direct", "impl", {"direct.codex": {"impl": "gpt-test:invalid-effort"}}),
            ("direct", "impl", {"direct.codex": {"impl": None}}),
            ("direct", "impl", {"assignments": {"direct": "unknown-family"}}),
            ("direct", "impl", {"direct.options": {"codex_service_tier": "unknown-tier"}}),
            ("producers", "morty", {"producers": {"morty": "codex:missing-agent:high"}}),
            ("producers", "morty", {"producers": {"morty": "codex:gpt-test:invalid-effort"}}),
            ("producers", "morty", {"producers": {"morty": None}}),
            ("producers", "morty", {"producers": {"morty": "unknown-family:gpt-test:high"}}),
            ("producers", "morty", {"producers": {"morty": "codex:gpt-test"}}),
            ("producers", "morty", {"producers": {"morty": "codex:gpt-test:"}}),
            ("producers", "morty", {"producers": {"morty": "codex::high"}}),
            ("producers", "morty", {"producers": {"morty": "codex:gpt-test:high:extra"}}),
        )
        for section, key, rows in cases:
            with self.subTest(section=section, key=key, rows=rows):
                _ = write_registry(self.path, rows)
                with self.assertRaises(ValueError):
                    _ = models_registry.launch_settings(section, key)

    def test_a_producer_row_is_validated_before_the_write_and_an_equal_row_changes_nothing(self) -> None:
        from scripts.lib import models_registry

        _ = write_registry(self.path, {"producers": {"morty": "codex:gpt-test:high", "brain": "codex:gpt-test:medium"}})
        unchanged = self.path.read_bytes()
        checked = models_registry.producer_settings("morty", "claude", "sonnet", "max")
        self.assertEqual(self.path.read_bytes(), unchanged)
        stored = models_registry.store_producer("morty", "claude", "sonnet", "max")
        self.assertEqual(stored, checked)
        self.assertEqual((stored.kind, stored.model, stored.effort), ("claude", "sonnet", "max"))
        self.assertEqual(models_registry.launch_settings("producers", "morty"), stored)
        self.assertEqual(models_registry.parse_registry(self.path.read_text())["producers"],
                         {"morty": "claude:sonnet:max", "brain": "codex:gpt-test:medium"})
        before = self.path.read_bytes(), self.path.stat().st_mtime_ns
        self.assertEqual(models_registry.store_producer("morty", "claude", "sonnet", "max"), stored)
        for family, model, effort in (("claude", "missing-agent", "max"), ("claude", "sonnet", "invalid-effort"),
                                      ("unknown-family", "sonnet", "max"), ("claude", "sonnet", "")):
            with self.subTest(family=family, model=model, effort=effort), self.assertRaises(ValueError):
                _ = models_registry.store_producer("morty", family, model, effort)
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_mtime_ns), before)
        for name, source in (
            ("existing producers", self.path.read_text()),
            ("empty producers before another section",
             "[producers]\n" + registry_text({"producers": ExactRows({})})),
        ):
            with self.subTest(group=name):
                _ = self.path.write_text(source)
                added = models_registry.store_producer("producer_only", "codex", "gpt-test", "high")
                self.assertEqual(models_registry.launch_settings("producers", "producer_only"), added)
                self.assertEqual(check_blank_lines(self.root, [Path(self.path.name)]), [])

    def test_compact_windows_give_only_a_role_with_a_key_its_window(self) -> None:
        _ = write_registry(self.path, {"compact_windows": {"producer": "300000"}})
        with redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(models_registry.compact_window_arguments("producer"), ["--autocompact", "300000"])
            for role in ("unit", "showrunner", "worker"):
                with self.subTest(role=role):
                    self.assertEqual(models_registry.compact_window_arguments(role), [])
        self.assertEqual(errors.getvalue(), "")
        _ = write_registry(self.path, {"compact_windows": {"producer": None}})
        self.assertEqual(models_registry.compact_window_arguments("producer"), [])

    def test_a_bad_compact_window_prints_one_anomaly_and_keeps_the_settings_json_window(self) -> None:
        from scripts.lib.test_names import fixture_names

        for value in ("", "30k", "3e5", "-300000", "300000.5", "99999", "1000001", "large", "0300000"):
            with self.subTest(value=value):
                _ = write_registry(self.path, {"compact_windows": {"producer": value}})
                with redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(models_registry.compact_window_arguments("producer"), [])
                lines = errors.getvalue().splitlines()
                self.assertEqual(len(lines), 1, lines)
                self.assertTrue(lines[0].startswith(f"anomaly: {self.path}: compact_windows.producer: "), lines[0])
                self.assertIn("--autocompact", lines[0])
                self.assertTrue(lines[0].endswith("Use the settings.json window."), lines[0])
        for value, expected in (("100000", "100000"), ("1000000", "1000000"), ("300K", "300k"), ("1m", "1M")):
            with self.subTest(value=value):
                _ = write_registry(self.path, {"compact_windows": {"producer": value}})
                self.assertEqual(models_registry.compact_window_arguments("producer"), ["--autocompact", expected])
        _ = write_registry(self.path, {"compact_windows": {"producer": "auto", "unknown": "300k"}})
        with (patch.object(names, "runtime_names", return_value=fixture_names(worker="operator")),
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(models_registry.compact_window_arguments("producer"), [])
        self.assertEqual(errors.getvalue().splitlines(), [
            f"anomaly: {self.path}: compact_windows.unknown: no role has this name. "
            + "Use producer, showrunner, unit, worker (operator)."])


@final
class Arguments(argparse.Namespace):
    path: Path = Path()
    rows: list[str] = []
    replace_section: list[str] = []
    bare_row: list[str] = []
    update: bool = False
    replace_provided_sections: bool = False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("path", type=Path)
    _ = parser.add_argument("rows", nargs="*", help="section/key=value; omit =value to delete")
    _ = parser.add_argument("--replace-section", action="append", default=[])
    _ = parser.add_argument("--bare-row", action="append", default=[], help="section/key without a value")
    _ = parser.add_argument("--update", action="store_true")
    _ = parser.add_argument("--replace-provided-sections", action="store_true",
                            help="replace the supplied sections for closed catalog/sweep scenarios")
    args = Arguments()
    _ = parser.parse_intermixed_args(namespace=args)
    rows: dict[str, dict[str, str | None]] = {}
    for argument in args.rows:
        address, separator, value = argument.partition("=")
        section, key = address.rsplit("/", 1)
        rows.setdefault(section, {})[key] = value if separator else None
    if args.replace_provided_sections:
        args.replace_section.extend(rows)
    _ = write_registry(args.path, rows, replace_sections=args.replace_section, update=args.update,
                       bare_rows=args.bare_row)


if __name__ == "__main__":
    main()
