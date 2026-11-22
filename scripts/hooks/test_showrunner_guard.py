"""Exercise both showrunner guard fronts with private staff and checkout fixtures."""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import cast, override

from scripts.agents import remote
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.production import staff
from scripts.production.staff_test_support import fixture_member, fixture_runtime_id, isolated_staff_file

ROOT = Path(__file__).resolve().parents[2]
CLAUDE_MATCHER = "Bash|Edit|Write|NotebookEdit|MultiEdit|Agent"
AGENT_REASON = (
    "An enrolled showrunner may write only through the permitted paths and routes below. "
    "Refuse built-in `Agent` launches, including read-only launches. "
    "Use a read-only review worker for reviews. "
    "Skills that need built-in `Agent` use the review worker route."
)

STUB = r'''#!__PYTHON__
import hashlib
import json
import os
import re
import signal
import sys
from pathlib import Path

home = os.environ["CODEX_HOME"]
state_files = json.loads(os.environ.get("STUB_ACCOUNT_STATES", "{}"))
state_file = Path(state_files.get(home, os.environ["STUB_STATE"]))
hooks_file = Path(home) / "hooks.json"

def load_state():
    if state_file.exists():
        return json.loads(state_file.read_text())
    return {"trust": {}, "calls": []}

def save_state(state):
    state_file.write_text(json.dumps(state))

def read_config(state, params):
    config = {"hooks": {"state": {key: {"trusted_hash": value} for key, value in state["trust"].items()}}}
    user_config = str(hooks_file.with_name("config.toml"))
    layer = {"name": {"type": "user", "file": user_config}, "version": "fixture",
             "config": config, "disabledReason": None}
    return {"config": config, "origins": {}, "layers": [layer] if params.get("includeLayers") else None}

def write_trust(state, edits):
    for edit in edits:
        path, strategy, value = edit["keyPath"], edit["mergeStrategy"], edit["value"]
        if path == "hooks.state" and strategy == "upsert":
            for key, record in value.items():
                state["trust"][key] = record["trusted_hash"]
        elif path.startswith("hooks.state.") and strategy == "replace":
            key = json.loads(path.removeprefix("hooks.state."))
            if value is None:
                state["trust"].pop(key, None)
            else:
                state["trust"][key] = value["trusted_hash"]
        else:
            raise RuntimeError("unexpected trust edit")

def hooks_list(state):
    if not hooks_file.exists():
        return {"data": [{"hooks": []}]}
    config = json.loads(hooks_file.read_text())
    for kind in ("warnings", "errors"):
        message = os.environ.get("STUB_LIST_" + kind.upper())
        if message:
            return {"data": [{"hooks": [], kind: [message]}]}
    entries = []
    for event, groups in config.get("hooks", {}).items():
        for group_index, group in enumerate(groups):
            matcher = group.get("matcher", "")
            for handler_index, handler in enumerate(group.get("hooks", [])):
                if "timeout" in handler and type(handler["timeout"]) is not int:
                    return {"data": [{"hooks": [], "warnings": [
                        f"failed to parse hooks config {hooks_file}: invalid type: map, expected u64"
                    ], "errors": []}]}
                event_key = re.sub(r"(?<!^)(?=[A-Z])", "_", event).lower()
                key = f"{hooks_file}:{event_key}:{group_index}:{handler_index}"
                material = json.dumps([event, matcher, handler], sort_keys=True)
                current_hash = hashlib.sha256(material.encode()).hexdigest()
                recorded_hash = state["trust"].get(key)
                status = ("untrusted" if recorded_hash is None else
                          "trusted" if recorded_hash == current_hash else "modified")
                entries.append({
                    "key": key, "eventName": event[:1].lower() + event[1:],
                    "matcher": None if event in ("UserPromptSubmit", "Stop") else matcher,
                    "handlerType": handler.get("type"),
                    "command": handler.get("command"),
                    "timeoutSec": handler.get("timeout"),
                    "sourcePath": str(hooks_file),
                    "currentHash": current_hash, "trustStatus": status,
                    "enabled": os.environ.get("STUB_DISABLED") != "1" and
                               os.environ.get("STUB_DISABLED_COMMAND") != handler.get("command"),
                })
                if os.environ.get("STUB_CHANGED_COMMAND") == handler.get("command"):
                    entries[-1][os.environ["STUB_LIST_FIELD"]] = json.loads(os.environ["STUB_LIST_VALUE"])
    return {"data": [{"hooks": entries, "warnings": [], "errors": []}]}

state = load_state()
state.setdefault("trust", {})
state.setdefault("calls", [])
state["argv"] = sys.argv[1:]
state["cwd"] = os.getcwd()
state["codex_home"] = os.environ["CODEX_HOME"]
save_state(state)
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request.get("method")
    params = request.get("params", {})
    state = load_state()
    state["calls"].append({"method": method, "params": params})
    save_state(state)
    if method == "initialize":
        result = {"userAgent": "stub"}
    elif method == "hooks/list":
        result = hooks_list(state)
    elif method == "config/read":
        result = read_config(state, params)
    elif method == "config/batchWrite":
        if os.environ.get("STUB_REFUSE") == "1":
            print(json.dumps({"id": request["id"], "error": {
                "code": -32602, "message": "invalid config key: hooks.state"
            }}), flush=True)
            continue
        write_trust(state, params["edits"])
        save_state(state)
        installer_pid = os.environ.get("STUB_INTERRUPT_INSTALLER")
        if installer_pid == str(os.getppid()):
            os.kill(int(installer_pid), signal.SIGKILL)
            raise SystemExit(0)
        result = {}
    else:
        print(json.dumps({"id": request["id"], "error": {
            "code": -32601, "message": "unknown method: " + str(method)
        }}), flush=True)
        continue
    print(json.dumps({"id": request["id"], "result": result}), flush=True)
'''


# The payload goes to the real front on stdin. The arguments supply only fixture dependencies.
DRIVER = """
import json, runpy, signal, sys, time
from pathlib import Path
from scripts.hooks import showrunner_guard as guard
from scripts.production import production_doc

front, fixtures, fault = sys.argv[1:]
paths = json.loads(fixtures)

class ProductionPathsError(ValueError):
    pass

class UnreadableRoot(type(Path())):
    def __hash__(self):
        raise TypeError("fixture root cannot be read")

def resolve(document):
    fields = paths.get(str(document))
    if fields == "unusable":
        raise ProductionPathsError("fixture checkout is unusable")
    if fields is None:
        return guard.NotEnrolled()
    main, *other = map(Path, fields)
    if fault == "target fact":
        main = UnreadableRoot(main)
    return guard.EnrolledPaths(main, *other)

def policy(enrolled, operations):
    if fault == "timeout":
        time.sleep(1)
        return guard.Allow()
    if fault == "error":
        raise RuntimeError("fixture policy error")
    return {"decision": "allow"}

if front in ("dispatch", "dispatch with refusal"):
    from scripts.hooks import hook_dispatch
    production_doc.LegacyProduction = guard.NotEnrolled
    production_doc.EnrolledPaths = guard.EnrolledPaths
    production_doc.resolve_production_paths = resolve
    registrations = (hook_dispatch.SHOWRUNNER_GUARD,)
    if front == "dispatch with refusal":
        registrations += (hook_dispatch.HookSpec(str(Path.cwd() / "refusing_hook.py"), "Bash"),)
    hook_dispatch.EVENTS["pre_tool_use"] = registrations
    sys.argv = ["hook_dispatch.py", "pre_tool_use"]
    hook_dispatch.main()

main = guard.main if front == "claude" else runpy.run_path(front)["main"]
dependencies = {"resolver": resolve}
if fault in ("timeout", "error", "invalid"):
    dependencies.update(policy=policy, policy_timeout_seconds=0.01)
if fault in ("unavailable cwd", "repeated cwd"):
    read_cwd = guard._cwd
    cwd_reads = 0

    def unreliable_cwd(payload):
        global cwd_reads
        cwd_reads += 1
        if fault == "unavailable cwd" or cwd_reads > 1:
            raise OSError("fixture cwd cannot be read")
        return read_cwd(payload)

    guard._cwd = unreliable_cwd
if fault == "file target":
    resolve_path = guard._path

    def unreadable_file(value, cwd):
        if Path(value).name in ("unreadable-target.py", "unreadable-parent"):
            raise TypeError("fixture path cannot be read")
        return resolve_path(value, cwd)

    guard._path = unreadable_file
if fault == "branch deadline":
    signal.setitimer(signal.ITIMER_REAL, guard.policy_timeout())
raise SystemExit(main(**dependencies))
"""


def json_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AssertionError(f"Expected a JSON object. Received {value!r}.")
    return cast(dict[str, object], value)


class GuardFixture(unittest.TestCase):
    root: Path = Path()
    home: Path = Path()
    main: Path = Path()
    other_main: Path = Path()
    outside: Path = Path()
    codex_home: Path = Path()
    environment: dict[str, str] = {}
    fixtures: dict[str, list[str] | str] = {}
    owner_notes: Path = Path()
    session_id: str = ""
    other_session: str = ""
    legacy_session: str = ""
    producer_session: str = ""
    project: Path = Path()
    transcript: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.home = self.root / "home"
        self.main = self.home / ".claude"
        self.other_main = self.root / "other"
        self.outside = self.root / "outside"
        self.codex_home = self.home / ".codex"
        configuration = self.main / "config"
        for folder in (configuration, self.other_main, self.outside, self.codex_home):
            folder.mkdir(parents=True)
        _ = (configuration / "showrunner_write_guard.conf").write_text(
            "# range: 0.001..\npolicy_timeout_seconds=2\n"
            + "# range: 0.001..\nreport_budget_seconds=0.1\n")
        self.environment = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TMPDIR") if key in os.environ}
        self.environment.update({
            "HOME": str(self.home), "CLAUDE_CONFIG_DIR": str(self.main),
            "CONF_DIR": str(configuration),
            "CODEX_HOME": str(self.codex_home), "XDG_STATE_HOME": str(self.home / ".local/state"),
            "XDG_CONFIG_HOME": str(self.home / ".config"), "SHUTDOWN_STATE_DIR": str(self.root / "shutdown"),
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(ROOT),
        })
        self.fixtures = {}
        with isolated_staff_file(self.root / "staff.json", self.environment):
            owner = self.member("owner", self.main)
            self.session_id = owner.current_session
            self.owner_notes = self.main / ".showrunner-notes" / owner.id
            self.other_session = self.member("other", self.other_main).current_session
            self.legacy_session = self.member("legacy", self.main, enrolled=False).current_session
            self.producer_session = self.member("producer", self.main, kind="producer").current_session
        project_key = re.sub(r"[^a-zA-Z0-9]", "-", str(self.main))
        self.project = self.main / "projects" / project_key
        self.transcript = self.project / f"{self.session_id}.jsonl"
        self.transcript.parent.mkdir(parents=True)
        _ = self.transcript.write_text(json.dumps({
            "sessionId": self.session_id, "cwd": str(self.main), "slug": "fixture-plan",
        }) + "\n")

    def member(self, name: str, main: Path, *, enrolled: bool = True, kind: staff.Kind = "showrunner") -> staff.StaffRow:
        doc = main / f"{name}-production-live.md"
        _ = doc.write_text(f"# {name}\n")
        row = fixture_member(kind, name=name, runtime_id=fixture_runtime_id(name),
                             doc=staff.LiveDoc(remote.local_machine(), doc))
        if enrolled:
            self.fixtures[str(doc)] = [str(path) for path in (
                main, doc, main / f"{name}-LOG.md", main / ".showrunner-notes" / row.id,
                self.root / f"{name}-integration",
            )]
        return row

    def run_front(self, front: str, tool: str, tool_input: Mapping[str, object], *,
                  session_id: str = "", cwd: Path | None = None, fault: str = "") -> subprocess.CompletedProcess[str]:
        payload = {
            "session_id": session_id or self.session_id, "cwd": str(cwd or self.main),
            "transcript_path": str(self.transcript), "hook_event_name": "PreToolUse",
            "tool_name": tool, "tool_input": tool_input,
        }
        return self.run_fixture(["-c", DRIVER, front, json.dumps(self.fixtures), fault], payload)

    def run_fixture(self, args: list[str], payload: Mapping[str, object] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, *args], input=json.dumps(payload), text=True,
                              capture_output=True, check=False, env=self.environment, cwd=self.root,
                              timeout=TEST_HANG_GUARD_SECS)

    def git(self, *args: str) -> str:
        result = subprocess.run(["git", "-C", str(self.main), *args], text=True, capture_output=True, check=True,
                                env=self.environment, timeout=TEST_HANG_GUARD_SECS)
        return result.stdout.strip()

    def create_main(self) -> None:
        _ = self.git("init", "--initial-branch=main")
        _ = self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                     "commit", "--allow-empty", "-m", "Fixture")

    def assert_allow(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def assert_deny(self, result: subprocess.CompletedProcess[str]) -> str:
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip(), f"The hook returned no decision. {result.stderr}")
        response = json_object(cast(object, json.loads(result.stdout)))
        decision = json_object(response["hookSpecificOutput"])
        self.assertEqual(decision["hookEventName"], "PreToolUse")
        self.assertEqual(decision["permissionDecision"], "deny")
        reason = decision["permissionDecisionReason"]
        self.assertIsInstance(reason, str)
        return cast(str, reason)

    def assert_unknown(self, result: subprocess.CompletedProcess[str], scope: str) -> None:
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        self.assertRegex(result.stderr, rf"^showrunner guard: unknown {scope}: [^\n]+\n\Z")


class ShowrunnerGuardTests(GuardFixture):
    codex_front: str = str(ROOT / "scripts/hooks/pre-tool-use-showrunner-guard-codex.py")

    def test_claude_checks_enrollment_and_each_protected_checkout(self) -> None:
        for label, session, target, denied in (
            ("own main", self.session_id, self.main / "source.py", True),
            ("other main", self.session_id, self.other_main / "source.py", True),
            ("other live document", self.session_id, self.other_main / "other-production-live.md", True),
            ("outside", self.session_id, self.outside / "source.py", False),
            ("legacy", self.legacy_session, self.main / "source.py", False),
            ("unknown session", fixture_runtime_id("unregistered"), self.main / "source.py", False),
            ("other staff role", self.producer_session, self.main / "source.py", False),
        ):
            with self.subTest(label=label):
                result = self.run_front("claude", "Write", {"file_path": str(target)}, session_id=session)
                if denied:
                    self.assertIn(str(target), self.assert_deny(result))
                else:
                    self.assert_allow(result)

    def test_claude_permits_only_owned_and_exact_runtime_files(self) -> None:
        _ = self.git("init", "--initial-branch=main")
        session = self.session_id
        team = self.main / "teams/team-one"
        team.mkdir(parents=True)
        _ = (team / "config.json").write_text(json.dumps({"leadSessionId": session}))
        for target in (
            self.main / "owner-production-live.md",
            self.main / "owner-LOG.md",
            self.owner_notes / "decision.md",
            self.project / "memory/context.md",
            self.main / "plans" / f"{session}.md",
            self.main / "plans/fixture-plan.md",
            self.main / "todos" / f"{session}.json",
            self.main / "tasks" / session / "1.json",
            team / "config.json",
            self.transcript,
            self.project / session / "tool-results/result.txt",
            self.main / "sessions" / f"{session}.json",
            self.main / "state" / session / "state.json",
            self.main / "history.jsonl",
            self.main / "file-history" / session / "edit",
            self.main / "session-env" / session / "env",
            self.main / "shell-snapshots/snapshot.sh",
            self.main / "paste-cache/paste.txt",
            self.main / "uploads/file.txt",
            self.main / "settings.local.json",
        ):
            with self.subTest(target=target.relative_to(self.main)):
                self.assert_allow(self.run_front("claude", "Edit", {"file_path": str(target)}))

        linked = self.outside / "linked"
        _ = self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                     "commit", "--allow-empty", "-m", "Fixture")
        _ = self.git("worktree", "add", "-b", "linked", str(linked))
        linked_config = self.home / ".claude2"
        linked_config.mkdir()
        (linked_config / "projects").symlink_to(self.project.parent)
        self.environment["CLAUDE_CONFIG_DIR"] = str(linked_config)
        transcript_folder = re.sub(r"[^a-zA-Z0-9]", "-", str(linked))
        transcript = self.project.parent / transcript_folder / self.transcript.name
        transcript.parent.mkdir()
        _ = transcript.write_text(json.dumps({"sessionId": session, "cwd": str(linked)}) + "\n")
        self.transcript: Path = linked_config / "projects" / transcript_folder / transcript.name
        with self.subTest(memory_project="linked checkout"):
            memory = linked_config / "projects" / self.project.name / "memory/context.md"
            for path in (memory, self.transcript, self.transcript.parent / session / "tool-results/result.txt"):
                self.assert_allow(self.run_front("claude", "Edit", {"file_path": str(path)}, cwd=linked))

    def test_claude_checks_runtime_ownership_and_the_settings_index(self) -> None:
        _ = self.git("init", "--initial-branch=main")
        settings = self.main / "settings.local.json"
        _ = settings.write_text("{}\n")
        _ = self.git("add", "--", settings.name)
        team = self.main / "teams/another-team"
        team.mkdir(parents=True)
        _ = (team / "config.json").write_text(json.dumps({"leadSessionId": self.other_session}))
        for target in (
            self.main / "projects/another-project/memory/context.md",
            self.main / "plans/unowned.md",
            self.main / "todos" / f"{self.other_session}.json",
            self.main / "tasks" / self.other_session / "1.json",
            team / "config.json",
            self.main / "sessions" / f"{self.other_session}.json",
            settings,
        ):
            with self.subTest(target=target.relative_to(self.main)):
                self.assertIn(str(target), self.assert_deny(self.run_front("claude", "Edit", {"file_path": str(target)})))

    def test_claude_refuses_protected_paths_after_resolution(self) -> None:
        _ = self.git("init", "--initial-branch=main")
        protected = self.main / "scripts/target.py"
        protected.parent.mkdir()
        _ = protected.write_text("fixture\n")
        alias = self.outside / "alias.py"
        alias.symlink_to(protected)
        nested_settings = self.main / ".claude/settings.local.json"
        nested_settings.parent.mkdir()
        _ = nested_settings.write_text("{}\n")
        _ = self.git("add", "--", ".claude/settings.local.json")
        _ = (self.main / ".gitignore").write_text("commands/\n")
        relative_base = self.home / "child"
        relative_base.mkdir()
        for target, cwd, canonical in (
            ("~/.claude/commands/new.md", self.outside, self.main / "commands/new.md"),
            ("../.claude/scripts/target.py", relative_base, protected),
            (str(alias), self.outside, protected),
            (str(self.main / "state" / self.other_session / "state.json"), self.main,
             self.main / "state" / self.other_session / "state.json"),
            (".claude/settings.local.json", self.main, nested_settings),
        ):
            with self.subTest(target=target):
                self.assertIn(str(canonical), self.assert_deny(
                    self.run_front("claude", "Edit", {"file_path": target}, cwd=cwd)))

    def test_claude_checks_notebooks_and_every_file_in_a_multi_edit(self) -> None:
        protected = self.main / "private.ipynb"
        for case, tool, fields in (
            ("notebook", "NotebookEdit", {"notebook_path": str(protected), "new_source": "1"}),
            ("later edit", "MultiEdit", {"file_path": str(self.outside / "notes.txt"),
                                        "edits": [{"old_string": "old", "new_string": "new"},
                                                  {"file_path": str(protected), "old_string": "old", "new_string": "new"}]}),
            ("unknown and protected", "Write", {"file_paths": [None, str(protected)]}),
        ):
            with self.subTest(case=case):
                self.assertIn(str(protected), self.assert_deny(self.run_front("claude", tool, fields)))

    def test_git_writes_and_publication_use_both_fronts(self) -> None:
        self.create_main()
        _ = self.git("config", "extensions.worktreeConfig", "true")
        linked = self.outside / "linked"
        _ = self.git("worktree", "add", "-b", "linked", str(linked))
        main = shlex.quote(str(self.main))
        git_dir = shlex.quote(str(self.main / ".git"))
        linked_git_dir = shlex.quote(self.git("-C", str(linked), "rev-parse", "--absolute-git-dir"))
        outside = shlex.quote(str(self.outside))
        publication = self.outside / "main_publish.py"
        _ = publication.write_text("# Fixture publication file.\n")
        alias = self.outside / "publish-alias"
        alias.symlink_to(publication)
        launcher = shlex.quote(str(self.main / "scripts/lib/py"))
        git_write = "git add source.py"
        nested_script = shlex.quote('git -C "$fixture_dir" add source.py')
        for front in ("claude", self.codex_front):
            for command, cwd, denied in (
                ("git commit -m fixture", self.main, True),
                (f"git -C {main} commit -m fixture", self.outside, True),
                (f"git -C {main} restore -- source.py", self.outside, True),
                (f"git -C {main} fetch origin main:refs/heads/fixture", self.outside, True),
                (f"git --git-dir={git_dir} update-ref refs/heads/fixture HEAD", self.outside, True),
                (f"git --work-tree={main} add file.txt", self.outside, True),
                (f"GIT_DIR={git_dir} git config core.filemode false", self.outside, True),
                (f"GIT_WORK_TREE={main} git add file.txt", self.outside, True),
                (f"git -C {outside} commit -m fixture", self.main, False),
                *((f"git -C {main} {read}", self.outside, False) for read in (
                    "status", "log", "diff", "show", "rev-parse HEAD", "grep example",
                    "ls-files", "cat-file -t HEAD", "fetch --no-tags --no-write-fetch-head --refmap= origin main",
                )),
                ("git push origin HEAD", self.outside, True),
                *((f"python3 scripts/production/{script}", self.outside, True)
                  for script in ("merge_checkpoint.py", "land_main.py", "main_publish.py")),
                ("touch ordinary.txt", self.main, False),
                (f"env -C {main} {git_write}", self.outside, True),
                (f"env --chdir={main} {git_write}", self.outside, True),
                (f"env -i -u FIXTURE GIT_DIR={git_dir} git update-ref refs/heads/main HEAD",
                 self.outside, True),
                (f"env -S {shlex.quote(f'git -C {main} add source.py')}", self.outside, True),
                *((f"{wrapper} {git_write}", self.main, True) for wrapper in (
                    "time -p timeout -k 1 5", "nice -n 5", "stdbuf -o L", "ionice -c 2 -n 7",
                    "xargs -n 1", "command -p", "exec -a fixture", "sudo -u fixture",
                )),
                (f"command cd {main} && {git_write}", self.outside, True),
                (f"builtin pushd {main}; builtin pushd {outside}; builtin popd; {git_write}", self.outside, True),
                *((f"{builtin} GIT_DIR={git_dir}; git update-ref refs/heads/main HEAD", self.outside, True)
                  for builtin in ("command export", "builtin declare -x")),
                (f"pushd {main}; popd -n; {git_write}", self.outside, True),
                (f"cd {outside} | cat; {git_write}", self.main, True),
                (f"cd {outside} & {git_write}", self.main, True),
                (f"cd {outside}; {git_write}", self.main, False),
                (f"cd {outside} || cd {main}; {git_write}", self.outside, True),
                (f"if fixture; then cd {main}; else cd {outside}; fi; {git_write}", self.outside, True),
                (f"case fixture in a) cd {main} ;; *) cd {outside} ;; esac; {git_write}", self.outside, True),
                (f"git clone fixture-source {main}", self.outside, True),
                ("git clone https://example.invalid/.claude.git", self.home, True),
                ("git update-ref refs/heads/main HEAD", linked, True),
                ("git submodule init", linked, True),
                ("git branch -m linked renamed", linked, True),
                ("git pack-refs --all", linked, True),
                (f"git --git-dir={linked_git_dir} commit -m linked", linked, False),
                (f"GIT_DIR={linked_git_dir} git commit -m linked", linked, False),
                (f"GIT_DIR={linked_git_dir} {git_write}", linked, False),
                ("git config --file get core.fixture value", self.main, True),
                ("git config --file fixture.conf get core.fixture", self.main, False),
                ("git config --get --replace-all core.fixture value", self.main, True),
                (f"./{alias.name}", self.outside, True),
                (f"python3 {alias.name}", self.outside, True),
                (f"{launcher} {alias.name}", self.outside, True),
                (f'export fixture_dir={main}; script={nested_script}; bash -c "$script"', self.outside, True),
            ):
                with self.subTest(front=Path(front).name, command=command):
                    result = self.run_front(front, "Bash", {"command": command}, cwd=cwd)
                    if denied:
                        _ = self.assert_deny(result)
                    else:
                        self.assert_allow(result)
            with self.subTest(front=Path(front).name, command="unknown wrapper option"):
                self.assert_unknown(self.run_front(
                    front, "Bash", {"command": f"env --fixture-option {git_write}"}), "target")
            with self.subTest(front=Path(front).name, command="trailing pipeline separator"):
                result = self.run_front(front, "Bash", {"command": "bash -s 0<&- |"})
                self.assert_unknown(result, "target")
                self.assertIn("expected a pipeline command", result.stderr)
            with self.subTest(front=Path(front).name, command="protected pipeline stage"):
                result = self.run_front(front, "Bash", {"command": f"printf fixture | {git_write}"}, cwd=self.main)
                self.assertIn("Git add changes a protected checkout", self.assert_deny(result))
            with self.subTest(front=Path(front).name, command="linked checkout metadata"):
                _ = self.assert_deny(self.run_front(
                    front, "Bash", {"command": "git config core.fixture value"}, cwd=linked))
                for command in ("git commit -m linked", "git config --worktree core.fixture value"):
                    self.assert_allow(self.run_front(front, "Bash", {"command": command}, cwd=linked))
            with self.subTest(front=Path(front).name, command="detach linked HEAD"):
                self.assert_allow(self.run_front(
                    front, "Bash", {"command": "git switch --detach HEAD"}, cwd=linked))
                _ = self.assert_deny(self.run_front(
                    front, "Bash", {"command": "git switch --detach HEAD"}, cwd=self.main))
                _ = self.assert_deny(self.run_front(
                    front, "Bash", {"command": "git switch -c another-branch HEAD"}, cwd=linked))
            with self.subTest(front=Path(front).name, command="branch deadline"):
                branches = [
                    f"if fixture; then FIXTURE_UNUSED_{index}=yes; else FIXTURE_UNUSED_{index}=no; fi"
                    for index in range(20)
                ]
                result = self.run_front(front, "Bash", {"command": "; ".join([*branches, git_write])},
                                        fault="branch deadline")
                self.assertIn("Git add changes a protected checkout", self.assert_deny(result))

            with self.subTest(front=Path(front).name, command="branch merges"):
                choices = [f"if fixture; then value{index}=a; else value{index}=b; fi" for index in range(10)]
                arguments = " ".join(f'"$value{index}"' for index in range(10))
                branches = "; ".join([
                    f"if fixture; then cd {main}; else cd {outside}; fi",
                    *choices, f"printf '%s' {arguments}",
                ])
                protected = f"git -C {main} add source.py"
                for script in (f"{branches}; {protected}", f"{protected}; {branches}"):
                    result = self.run_front(front, "Bash", {"command": script}, cwd=self.outside,
                                            fault="branch deadline")
                    self.assertIn("Git add changes a protected checkout", self.assert_deny(result))

        _ = self.git("config", "--unset", "extensions.worktreeConfig")
        for front in ("claude", self.codex_front):
            with self.subTest(front=Path(front).name, command="shared config without worktree extension"):
                _ = self.assert_deny(self.run_front(
                    front, "Bash", {"command": "git config --worktree core.fixture value"}, cwd=linked))

    def test_shell_limits_keep_completed_git_writes(self) -> None:
        recursive = 'bash -c "$script"'
        for front in ("claude", self.codex_front):
            for tail, limit in (
                ("# " + "x" * 140_000, "work limit"),
                ("(" * 40 + "true" + ")" * 40, "nesting limit"),
                (f"script={shlex.quote(recursive)}; {recursive}", "nesting limit"),
            ):
                with self.subTest(front=Path(front).name, suffix=tail[:55]):
                    script = f"git add source.py; {tail}"
                    result = self.run_front(front, "Bash", {"command": script})
                    self.assertIn("Git add changes a protected checkout", self.assert_deny(result))
                    result = self.run_front(front, "Bash", {"command": tail})
                    self.assert_unknown(result, "target")
                    self.assertIn(limit, result.stderr)

            with self.subTest(front=Path(front).name, suffix="later heredoc"):
                script = "cat <<FIRST; cat <<LATE\n$(git add source.py)\nFIRST\n" + "x" * 140_000 + "\nLATE"
                result = self.run_front(front, "Bash", {"command": script})
                self.assertIn("Git add changes a protected checkout", self.assert_deny(result))

    def test_agent_refusal_names_the_review_route_and_permitted_paths(self) -> None:
        result = self.run_front("claude", "Agent", {"subagent_type": "Explore"}, cwd=self.outside)
        reason = self.assert_deny(result)
        self.assertIn(AGENT_REASON, reason)
        self.assertIn("LOG", reason)
        self.assertIn("notes", reason)
        self.assertIn("merge unit", reason)

    def test_policy_faults_depend_on_known_enrollment_and_target(self) -> None:
        for front in ("claude", self.codex_front):
            for fault, detail in (("timeout", "tim"), ("error", "fixture policy error"), ("invalid", "invalid")):
                with self.subTest(front=Path(front).name, fault=fault, scope="protected"):
                    result = self.run_front(front, "Bash", {"command": "git add source.py"}, fault=fault)
                    self.assertIn(detail, self.assert_deny(result).lower())
                with self.subTest(front=Path(front).name, fault=fault, scope="unknown target"):
                    result = self.run_front(front, "Bash", {}, fault=fault)
                    self.assert_unknown(result, "target")
                with self.subTest(front=Path(front).name, fault=fault, scope="unknown identity"):
                    result = self.run_front(front, "Bash", {"command": "git add source.py"}, session_id="?", fault=fault)
                    self.assert_unknown(result, "identity")
                with self.subTest(front=Path(front).name, fault=fault, scope="outside"):
                    self.assert_allow(self.run_front(front, "Bash", {"command": "git add source.py"},
                                                     cwd=self.outside, fault=fault))
            with self.subTest(front=Path(front).name, fault="target fact"):
                result = self.run_front(front, "Edit" if front == "claude" else "Bash",
                                        {"command": "git add source.py",
                                         "file_path": str(self.main / "source.py")}, fault="target fact")
                self.assert_unknown(result, "target")
                self.assertIn("fixture root cannot be read", result.stderr)
            with self.subTest(front=Path(front).name, fault="repeated cwd"):
                result = self.run_front(front, "Bash", {"command": "git add source.py"}, fault="repeated cwd")
                self.assertIn("Git add changes a protected checkout", self.assert_deny(result))
            protected = self.main / "source.py"
            unreadable = self.outside / "unreadable-target.py"
            partial = self.main / "unreadable-parent" / "source.py"
            for paths, known in (
                ((unreadable, protected), protected),
                ((protected, unreadable), protected),
                ((partial,), partial),
            ):
                with self.subTest(front=Path(front).name, targets=paths):
                    if front == "claude":
                        fields = {"file_paths": [str(path) for path in paths]}
                        tool = "MultiEdit"
                    else:
                        fields = {"patch": "*** Begin Patch\n" + "".join(
                            f"*** Add File: {path}\n+fixture\n" for path in paths
                        ) + "*** End Patch\n"}
                        tool = "apply_patch"
                    result = self.run_front(front, tool, fields, cwd=self.outside, fault="file target")
                    self.assertIn(str(known), self.assert_deny(result))
        with self.subTest(fault="unavailable cwd", tool="Agent"):
            result = self.run_front("claude", "Agent", {"subagent_type": "Explore"}, fault="unavailable cwd")
            self.assertIn(AGENT_REASON, self.assert_deny(result))

    def test_resolver_errors_refuse_known_protection_and_report_unknown_target(self) -> None:
        owner_doc = str(self.main / "owner-production-live.md")
        other_doc = str(self.other_main / "other-production-live.md")
        owner_paths = self.fixtures[owner_doc]
        other_paths = self.fixtures[other_doc]
        command = {"command": "git add source.py"}
        for front in ("claude", self.codex_front):
            with self.subTest(front=Path(front).name):
                self.fixtures[owner_doc] = "unusable"
                self.fixtures[other_doc] = other_paths
                result = self.run_front(front, "Bash", command, cwd=self.other_main)
                self.assertIn("fixture checkout is unusable", self.assert_deny(result))
                self.fixtures[owner_doc] = owner_paths
                self.fixtures[other_doc] = "unusable"
                result = self.run_front(front, "Bash", command, cwd=self.other_main)
                self.assert_unknown(result, "target")
                self.assertIn("fixture checkout is unusable", result.stderr)
            self.fixtures[owner_doc] = "unusable"
            self.fixtures[other_doc] = other_paths
            for case, fields, cwd in (("outside", command, self.outside), ("no target", {}, self.main)):
                with self.subTest(front=Path(front).name, caller_unusable=case):
                    result = self.run_front(front, "Bash", fields, cwd=cwd)
                    self.assert_unknown(result, "target")
                    self.assertIn("fixture checkout is unusable", result.stderr)
            with self.subTest(front=Path(front).name, caller_unusable="publication"):
                result = self.run_front(front, "Bash", {"command": "git push origin HEAD"}, cwd=self.outside)
                self.assertIn("serving merge unit", self.assert_deny(result))
        with self.subTest(caller_unusable="Agent"):
            result = self.run_front("claude", "Agent", {"subagent_type": "Explore"}, cwd=self.outside)
            self.assertIn(AGENT_REASON, self.assert_deny(result))


class GuardReadinessTests(GuardFixture):
    def commit_fixture_main(self) -> str:
        self.create_main()
        installed_files = (
            "scripts/hooks/showrunner_guard.py",
            "scripts/hooks/pre-tool-use-showrunner-guard-codex.py",
            "scripts/hooks/hook_dispatch.py",
            "config/showrunner_write_guard.conf",
        )
        for filename in installed_files:
            target = self.main / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            _ = target.write_bytes((ROOT / filename).read_bytes())
        _ = self.git("add", "--", *installed_files)
        _ = self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                     "commit", "-m", "Install the fixture guard")
        launcher = self.main / "scripts/lib/py"
        launcher.parent.mkdir()
        launcher.symlink_to(ROOT / "scripts/lib/py")
        return self.git("rev-parse", "HEAD")

    def prepare_app_server(self) -> None:
        app_server = self.root / "codex"
        _ = app_server.write_text(STUB.replace("__PYTHON__", sys.executable))
        app_server.chmod(0o755)
        state = self.root / "codex-state.json"
        self.environment.update(CODEX_BIN=str(app_server), STUB_STATE=str(state))

    def trust_registrations(self, homes: tuple[Path, ...]) -> None:
        trusted: dict[str, str] = {}
        for home in homes:
            result = subprocess.run(
                [self.environment["CODEX_BIN"], "app-server"],
                input='{"id":1,"method":"hooks/list","params":{}}\n', text=True,
                capture_output=True, check=True, env={**self.environment, "CODEX_HOME": str(home)},
                cwd=self.home, timeout=TEST_HANG_GUARD_SECS,
            )
            response = json_object(cast(object, json.loads(result.stdout)))
            workspaces = cast(list[dict[str, list[dict[str, str]]]], json_object(response["result"])["data"])
            trusted.update((hook["key"], hook["currentHash"]) for hook in workspaces[0]["hooks"])
        _ = Path(self.environment["STUB_STATE"]).write_text(json.dumps({"trust": trusted}))

    def record_ready(self, settings: tuple[Path, Path], codex_homes: tuple[Path, Path],
                     destination: Path) -> subprocess.CompletedProcess[str]:
        return self.run_fixture([
            "-c",
            """import sys
from pathlib import Path
from scripts.hooks.showrunner_guard import record_ready
checkout, destination, first_settings, second_settings, first_codex, second_codex = map(Path, sys.argv[1:])
raise SystemExit(record_ready(checkout, (first_settings, second_settings), (first_codex, second_codex), destination))
""",
            *(str(path) for path in (self.main, destination, *settings, *codex_homes)),
        ])

    def test_record_ready_replaces_the_proof_only_with_all_registrations(self) -> None:
        revision = self.commit_fixture_main()
        settings = (self.main / "settings.json", self.home / ".claude-linked/settings.json")
        codex_homes = (self.codex_home, self.home / ".codex-linked")
        settings[1].parent.mkdir()
        codex_homes[1].mkdir()
        destination = self.home / ".local/state/showrunner/guard-ready.json"
        destination.parent.mkdir(parents=True)
        _ = destination.write_text('{"version":0}\n')
        claude_command = '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/hook_dispatch.py" pre_tool_use'
        codex_command = ('"$HOME/.claude/scripts/lib/py" '
                         '"$HOME/.claude/scripts/hooks/pre-tool-use-showrunner-guard-codex.py"')
        claude_registration = [{"matcher": CLAUDE_MATCHER, "hooks": [
            {"type": "command", "command": claude_command, "timeout": 30}]}]
        codex_registrations = [{"matcher": tool, "hooks": [{"type": "command", "command": codex_command, "timeout": 30}]}
                               for tool in ("Bash", "apply_patch")]
        _ = settings[0].write_text(json.dumps({"hooks": {"PreToolUse": claude_registration}}))
        _ = (codex_homes[0] / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": codex_registrations}}))
        self.prepare_app_server()
        guard_path = "scripts/hooks/showrunner_guard.py"
        changed_path = "scripts/hooks/pre-tool-use-showrunner-guard-codex.py"
        changed_file = self.main / changed_path
        installed = changed_file.read_bytes()
        empty_revision = self.git("rev-parse", "HEAD^")
        for case, diagnostic in (
            ("missing Claude", str(settings[1])),
            ("missing Codex", str(codex_homes[1])),
            ("untrusted Codex", "untrusted"),
            ("disabled Codex", "enabled"),
            ("short Claude timeout", "timeout"),
            ("short Codex timeout", "timeout"),
            ("main has no guard files", guard_path),
            ("installed file differs", changed_path),
            ("ready", ""),
        ):
            with self.subTest(case=case):
                claude_timeout = 2 if case == "short Claude timeout" else 30
                codex_timeout = 2 if case == "short Codex timeout" else 30
                claude_groups = [{"matcher": CLAUDE_MATCHER, "hooks": [
                    {"type": "command", "command": claude_command, "timeout": claude_timeout}]}]
                codex_groups = [
                    {"matcher": tool, "hooks": [
                        {"type": "command", "command": codex_command, "timeout": codex_timeout}]}
                    for tool in ("Bash", "apply_patch")
                ]
                _ = settings[1].write_text(json.dumps({"hooks": {
                    "PreToolUse": [] if case == "missing Claude" else claude_groups}}))
                _ = (codex_homes[1] / "hooks.json").write_text(json.dumps({"hooks": {
                    "PreToolUse": codex_groups[:1] if case == "missing Codex" else codex_groups}}))
                self.trust_registrations(codex_homes[:1] if case == "untrusted Codex" else codex_homes)
                if case == "disabled Codex":
                    self.environment["STUB_DISABLED_COMMAND"] = codex_command
                if case == "main has no guard files":
                    _ = self.git("update-ref", "refs/heads/main", empty_revision, revision)
                if case == "installed file differs":
                    _ = changed_file.write_bytes(installed + b"\n# Different installed file.\n")
                _ = destination.write_text('{"version":0}\n')
                try:
                    with destination.open() as original:
                        result = self.record_ready(settings, codex_homes, destination)
                        self.assertEqual(original.read(), '{"version":0}\n')
                finally:
                    _ = self.environment.pop("STUB_DISABLED_COMMAND", None)
                    if case == "main has no guard files":
                        _ = self.git("update-ref", "refs/heads/main", revision, empty_revision)
                    if case == "installed file differs":
                        _ = changed_file.write_bytes(installed)
                if diagnostic:
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn(diagnostic, result.stderr)
                    self.assertEqual(destination.read_text(), '{"version":0}\n')
                    continue
                self.assertEqual(result.returncode, 0, result.stderr)
                proof = json_object(cast(object, json.loads(destination.read_text())))
                self.assertEqual((proof["version"], proof["revision"]), (1, revision))
                recorded_at = datetime.fromisoformat(cast(str, proof["recorded_at"]))
                self.assertEqual(recorded_at.utcoffset(), UTC.utcoffset(recorded_at))
                self.assertCountEqual(cast(list[dict[str, str]], proof["claude"]), [{
                    "settings": str(path), "event": "PreToolUse",
                    "matcher": CLAUDE_MATCHER, "command": claude_command,
                } for path in settings])
                codex = cast(list[dict[str, str]], proof["codex"])
                self.assertCountEqual(codex, [{"codex_home": str(home), "event": "PreToolUse",
                                               "tool": tool, "command": codex_command}
                                              for home in codex_homes for tool in ("Bash", "apply_patch")])

    def test_record_ready_discovers_installed_client_registrations(self) -> None:
        revision = self.commit_fixture_main()
        override_claude = self.root / "claude-config"
        override_codex = self.root / "codex-config"
        linked_claude = self.home / ".claude2"
        numbered_codex = self.home / ".codex2"
        claude_dirs = (self.main, linked_claude, override_claude)
        codex_homes = (self.codex_home, numbered_codex, override_codex)
        for folder in (override_claude, override_codex, linked_claude, numbered_codex):
            folder.mkdir()
        self.environment.update(CLAUDE_CONFIG_DIR=str(override_claude), CODEX_HOME=str(override_codex))

        hooks = self.main / "scripts/hooks"
        guard = hooks / "showrunner_guard.py"
        claude_command = '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/hook_dispatch.py" pre_tool_use'
        codex_command = ('"$HOME/.claude/scripts/lib/py" '
                         '"$HOME/.claude/scripts/hooks/pre-tool-use-showrunner-guard-codex.py"')
        for folders, filename, matcher, command in (
            (claude_dirs, "settings.json", CLAUDE_MATCHER, claude_command),
            (codex_homes, "hooks.json", "Bash|apply_patch", codex_command),
        ):
            registration = {"hooks": {"PreToolUse": [{"matcher": matcher, "hooks": [
                {"type": "command", "command": command, "timeout": 30},
            ]}]}}
            for folder in folders:
                _ = (folder / filename).write_text(json.dumps(registration))
        self.prepare_app_server()
        self.trust_registrations(codex_homes)

        destination = self.home / ".local/state/showrunner/guard-ready.json"
        destination.parent.mkdir(parents=True)
        _ = destination.write_text('{"version":0}\n')
        driver = """import runpy, sys
from scripts.hooks import hook_dispatch
guard, registration = sys.argv[1:]
spec = hook_dispatch.SHOWRUNNER_GUARD
other_hooks = tuple(hook for hook in hook_dispatch.PRE_TOOL_USE if hook != spec)
hook_dispatch.PRE_TOOL_USE = (*other_hooks, spec) if registration == "installed" else other_hooks
sys.argv = [guard, "record-ready"]
runpy.run_path(guard, run_name="__main__")
"""
        result = self.run_fixture(["-c", driver, str(guard), "missing"])
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("Claude dispatcher has no showrunner guard registration", result.stderr)
        self.assertEqual(destination.read_text(), '{"version":0}\n')

        result = self.run_fixture(["-c", driver, str(guard), "installed"])
        self.assertEqual(result.returncode, 0, result.stderr)
        proof = json_object(cast(object, json.loads(destination.read_text())))
        self.assertEqual(proof["revision"], revision)
        self.assertCountEqual(cast(list[dict[str, str]], proof["claude"]), [{
            "settings": str(folder / "settings.json"), "event": "PreToolUse",
            "matcher": CLAUDE_MATCHER, "command": claude_command,
        } for folder in claude_dirs])
        self.assertCountEqual(cast(list[dict[str, str]], proof["codex"]), [{
            "codex_home": str(home), "event": "PreToolUse", "tool": tool, "command": codex_command,
        } for home in codex_homes for tool in ("Bash", "apply_patch")])


if __name__ == "__main__":
    _ = unittest.main()
