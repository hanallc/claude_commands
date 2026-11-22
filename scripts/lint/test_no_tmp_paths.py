"""Check explicit destinations for file and folder creation."""

from __future__ import annotations

import ast
from collections.abc import Iterator
import os
from pathlib import Path
import posixpath
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]
TEMP_PATH = re.compile(r"(?:/private)?/tmp(?=/|$)")
TEMP_PREFIX_INDEX = {"mkdtemp": 1, "mkstemp": 1, "mktemp": 1, "TemporaryDirectory": 1,
                     "TemporaryFile": 5, "NamedTemporaryFile": 5, "SpooledTemporaryFile": 6}
PATH_WRITERS = {"mkdir", "makedirs", "touch", "open", "write_text", "write_bytes"}
SHELL_WORD = r"""(?:\\.|'[^']*'|"(?:\\.|[^"\\])*"|[^\s"';&|<>()\\])+"""
SHELL_PATH = re.compile(r"""(?:--tmpdir=)?["']?(?:\${[\w${}:-]*:?-)?((?:/private)?/tmp(?=[/}"']|$)[^"']*)["']?""")
SHELL_WRITE = re.compile(
    rf"(?:^|(?<=[;(\n&|]))[ \t\v]*(?:(?:if|then|do|else|command|!) +)*(?P<tool>mkdir|mktemp|touch)"
    + rf"(?P<operands>(?:[ \t\v]+(?!#){SHELL_WORD})+)"
    + rf"|(?<![<>])(?:<>|>>|>\||>)[ \t\v]*(?P<redirect>{SHELL_WORD})"
    + r'|"\$\((?P<substitution>[^)\n]+)\)"'
    + r"""|(?<!<)<<-?[ \t\v]*['"]?(?P<end>\w+)['"]?(?P<header>[^\n]*)\n(?s:.*?)^[\t]*(?P=end)$"""
    + r"""|\[\[(?s:.*?)\]\]|(?<!\S)\#.*|'[^']*'|"(?:\\.|[^"\\])*" """.rstrip(),
    re.MULTILINE,
)
LAUNCHD_WRITE = re.compile(
    r"<!--.*?-->|<key>\s*(Standard(?:Out|Error)Path)\s*</key>\s*"
    + r"<string>\s*((?:/private)?/tmp(?:/[^<]*)?)</string>", re.DOTALL,
)

# Include the runtime destinations that use a variable or a separate writer.
ALLOWED_DESTINATIONS = {
    ("agents/com.natemccoy.codex-agent-catalog-sync.plist", "StandardErrorPath /tmp/codex-agent-catalog-sync/launchd.stderr.log"): "Launchd writes the catalog sync log.",
    ("agents/setup.sh", "/tmp/codex-agent-catalog-sync"): "The installer prepares the catalog sync log folder.",
    ("claude_to_codex/com.natemccoy.claude-to-codex-sync.plist", "StandardOutPath /tmp/claude-to-codex-sync/launchd.stdout.log"): "Launchd writes the sync output.",
    ("claude_to_codex/com.natemccoy.claude-to-codex-sync.plist", "StandardErrorPath /tmp/claude-to-codex-sync/launchd.stderr.log"): "Launchd writes the sync errors.",
    ("claude_to_codex/run_sync.sh", "/tmp/claude-to-codex-sync"): "The sync runner shares a lock and a log.",
    ("claude_to_codex/setup.sh", "/tmp/claude-to-codex-sync"): "The installer prepares the sync log folder.",
    ("hana_shot/hana_shot.py", "/tmp/hana_shot_"): "The remote machine stages the screenshot.",
    ("hooks/post-tool-use-context-usage.py", "/tmp/claude/context-hook-debug.jsonl"): "The context hook appends debug records.",
    ("lib/session_folder.py", "/tmp/claude/session"): "The former root supplies session restore paths.",
    ("lib/unit_runs.py", "/tmp/claude/scratch"): "The former root supplies the fallback for run folders.",
    ("lib/unit_runs.py", "/tmp/claude/astra"): "Astra reviews share a runtime folder.",
    ("lint/invoke.sh", "/tmp}/nextest_tokens."): "The nextest processes share the width file.",
    ("lint/invoke.sh", "/tmp}}"): "The sweep stores the previous run time.",
    ("lint/memory_gate.sh", "mktemp /tmp}/hold-check.XXXXXX"): "Memory admission stores diagnostic output.",
    ("message/send.py", "TemporaryDirectory /tmp"): "The sender uses a short Unix socket path.",
    ("prioritize/com.natemccoy.hanadocs-prioritize.plist", "StandardOutPath /tmp/hanadocs-prioritize/launchd.stdout.log"): "Launchd writes the watcher output.",
    ("prioritize/com.natemccoy.hanadocs-prioritize.plist", "StandardErrorPath /tmp/hanadocs-prioritize/launchd.stderr.log"): "Launchd writes the watcher errors.",
    ("prioritize/install_watcher.sh", "/tmp/hanadocs-prioritize"): "The installer prepares the watcher state folder.",
    ("prioritize/run_watcher.sh", "/tmp/hanadocs-prioritize"): "The watcher shares status, a lock, and a log.",
    ("prioritize/status_watcher.sh", "/tmp/hanadocs-prioritize"): "The status command reads the watcher files.",
    ("prioritize/writer_lock.py", "/tmp/hanadocs-prioritize/writer.lock"): "The watcher serializes changes with a lock.",
    ("sccache/com.natemccoy.sccache.plist", "StandardOutPath /tmp/sccache-launchd.log"): "Launchd writes the sccache output.",
    ("sccache/com.natemccoy.sccache.plist", "StandardErrorPath /tmp/sccache-launchd.log"): "Launchd writes the sccache errors.",
    ("settings/ensure_git_filters.sh", "StandardErrorPath /tmp/claude-settings-git-refresh.stderr.log"): "Launchd writes the Git filter refresh errors.",
    ("worktree_delete/confirm_gate.sh", "/tmp/claude/worktree_delete"): "The worktree commands share a confirmation nonce.",
}

def python_path(node: ast.AST | None) -> str:
    if isinstance(node, ast.Constant) and isinstance(value := node.value, str):
        return value
    if isinstance(node, ast.JoinedStr) and node.values:
        return python_path(node.values[0])
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return posixpath.join(python_path(node.left), python_path(node.right))
    if isinstance(node, ast.Call) and ast.unparse(node.func) in {"Path", "pathlib.Path"} and node.args:
        return posixpath.join(*(python_path(arg) for arg in node.args))
    return ""

def python_writes(source: str) -> Iterator[tuple[int, str]]:
    for call in (node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Call)):
        function = call.func
        name = function.attr if isinstance(function, ast.Attribute) else function.id if isinstance(function, ast.Name) else ""
        if name not in TEMP_PREFIX_INDEX and name not in PATH_WRITERS:
            continue
        args = dict(enumerate(call.args)) | {arg.arg: arg.value for arg in call.keywords}
        receiver = python_path(function.value) if isinstance(function, ast.Attribute) else ""
        if name in TEMP_PREFIX_INDEX:
            index = TEMP_PREFIX_INDEX[name]
            folder = args.get("dir", args.get(index + 1))
            prefix = python_path(args.get("prefix", args.get(index)))
            target = prefix if re.match(r"(?:/private)?/tmp/", prefix) else python_path(folder)
        else:
            target = receiver or python_path(args.get("name", args.get("file", args.get(0))))
            mode = python_path(args.get("mode", args.get(0 if receiver else 1)))
            if name == "open" and not re.search("[wax]", mode):
                continue
        if TEMP_PATH.match(target):
            yield call.lineno, f"{name} {target}"

def writes(filename: str, source: str) -> Iterator[tuple[int, str]]:
    if filename.endswith(".py"):
        yield from python_writes(source)
    if filename.endswith((".sh", ".zsh")) or re.match(r"#![^\n]*\b(?:ba|da|z|k)?sh\b", source):
        for match in SHELL_WRITE.finditer(source.replace("\\\n", " \v")):
            line = source.count("\n", 0, match.start()) + 1
            if inline := match["substitution"] or match["header"]:
                yield from ((line, target) for _, target in writes(filename, inline))
            for word in re.finditer(SHELL_WORD, match["operands"] or match["redirect"] or ""):
                if target := SHELL_PATH.fullmatch(word[0]):
                    yield line, f"{match['tool'] or 'redirect'} {target[1]}"
    if filename.endswith((".plist", ".sh", ".zsh")):
        for match in LAUNCHD_WRITE.finditer(source):
            if match[1]:
                yield source.count("\n", 0, match.start()) + 1, f"{match[1]} {match[2].rstrip()}"

class NoTemporaryPathsTests(unittest.TestCase):
    def test_recognizes_creation_syntax(self) -> None:
        samples = {
            "sample.py": (['tempfile.TemporaryDirectory(dir="/tmp")', 'mkdtemp(prefix="/tmp/work-")',
                           'os.mkdir("/tmp/work")', 'os.makedirs("/private/tmp/work")', 'Path("/tmp/work").mkdir()',
                           'Path("/tmp/note").write_text("hello")', 'open("/tmp/file", "w")'],
                          ['path = Path("/tmp/work")', 'assert Path("/tmp/work").exists()',
                           '# Path("/tmp/work").mkdir()', '"""os.mkdir("/tmp/work")"""', 'open("/tmp/file", "r")',
                           'output.write_text("mkdir /tmp/work")', '(Path("/tmp") / "/managed/file").touch()']),
            "sample.sh": (['mkdir -p "/tmp/work"', 'x=$(mktemp "${TMPDIR:-/tmp}/x.XXXXXX")',
                           'touch primary /private/tmp/stamp', 'printf hi >> /tmp/out',
                           'x="$(mktemp "/tmp/x.XXXXXX")"', "cat <<'EOF' > /tmp/out\ndata\nEOF"],
                          ['echo "mkdir /tmp/work"', 'echo "> /tmp/out"', '# mkdir /tmp/work',
                           'input=/tmp/work', 'cat < /tmp/in', 'echo mkdir /tmp/work',
                           '[[ "$value" > /tmp/data ]]', 'mkdir relative # /tmp/data', "cat <<'EOF'\ntouch /tmp/data\nEOF"]),
            "sample.plist": (['<key>StandardOutPath</key>\n<string>/tmp/out</string>',
                              '<key>StandardErrorPath</key><string>/private/tmp/err</string>'],
                             ['<key>WorkingDirectory</key><string>/tmp/work</string>']),
        }
        for filename, (creation, data) in samples.items():
            for source in creation + data:
                self.assertEqual(bool(list(writes(filename, source))), source in creation, source)

    def test_scripts_use_approved_temporary_paths(self) -> None:
        command = ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "scripts"]
        paths = subprocess.check_output(command, cwd=ROOT)
        allowed = set(ALLOWED_DESTINATIONS)
        unexpected: list[str] = []
        for filename in sorted({os.fsdecode(name) for name in paths.split(b"\0") if name}):
            path = ROOT / filename
            if not path.is_file() or path.is_symlink() or b"/tmp" not in (content := path.read_bytes()):
                continue
            for line, destination in writes(filename, content.decode("utf-8", errors="surrogateescape")):
                key = (filename.removeprefix("scripts/"), destination)
                if key not in allowed:
                    unexpected.append(f"{filename}:{line}: {destination}")
                allowed.discard(key)
        self.assertFalse(unexpected, "\n" + "\n".join(unexpected))
