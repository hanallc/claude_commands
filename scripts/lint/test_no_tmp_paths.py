"""Check explicit destinations for file and folder creation."""

from __future__ import annotations

import ast
from collections.abc import Iterator
from io import StringIO
import os
from pathlib import Path
import posixpath
import re
import subprocess
import tokenize
import unittest

ROOT = Path(__file__).resolve().parents[2]
TEMP_PATH = re.compile(r"(?:/private)?/tmp(?=/|$)")
TEMP_PREFIX_INDEX = {"mkdtemp": 1, "mkstemp": 1, "mktemp": 1, "TemporaryDirectory": 1,
                     "TemporaryFile": 5, "NamedTemporaryFile": 5, "SpooledTemporaryFile": 6}
PATH_WRITERS = {"mkdir", "makedirs", "touch", "open", "write_text", "write_bytes"}
PYTHON_WRITER = re.compile(r"\b(?:" + "|".join(sorted(PATH_WRITERS | TEMP_PREFIX_INDEX.keys())) + r")\b")
PYTHON_PATH_LITERAL = re.compile(r"""["'](?:\\\n)*(?:/private)?/tmp""")
SHELL_WORD = r"""(?:\\.|'[^']*'|"(?:\\.|[^"\\])*"|[^\s"';&|<>()\\])+"""
SHELL_PATH = re.compile(r"""(?:--tmpdir=)?["']?(?:\${[\w${}:-]*:?-)?((?:/private)?/tmp(?=[/}"']|$)[^"']*)["']?""")
SHELL_WRITE = re.compile(
    rf"(?:^|(?<=[;(\n&|]))[ \t\v]*(?:(?:if|then|do|else|command|!) +)*(?P<tool>mkdir|mktemp|touch|install)"
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

ALLOWED_DESTINATIONS = {
    ("agents/com.natemccoy.codex-agent-catalog-sync.plist", "StandardErrorPath /tmp/codex-agent-catalog-sync/launchd.stderr.log"): "Launchd writes the catalog sync log.",
    ("berth/install/install.sh", "mktemp /tmp}/cargo-berth-install.XXXXXX"): "Writes under the caller's TMPDIR; the fallback is roots.system.",
    ("claude_to_codex/com.natemccoy.claude-to-codex-sync.plist", "StandardOutPath /tmp/claude-to-codex-sync/launchd.stdout.log"): "Launchd writes the sync output.",
    ("claude_to_codex/com.natemccoy.claude-to-codex-sync.plist", "StandardErrorPath /tmp/claude-to-codex-sync/launchd.stderr.log"): "Launchd writes the sync errors.",
    ("direct/verify.sh", "mktemp /tmp}}/step-output.XXXXXX"): "Writes under the caller's TMPDIR; the fallback is roots.system.",
    ("direct/verify.sh", "mktemp /tmp}}/clippy.XXXXXX"): "Writes under the caller's TMPDIR; the fallback is roots.system.",
    ("lint/memory_gate.sh", "mktemp /tmp}/hold-check.XXXXXX"): "Memory admission stores diagnostic output.",
    ("prioritize/com.natemccoy.hanadocs-prioritize.plist", "StandardOutPath /tmp/hanadocs-prioritize/launchd.stdout.log"): "Launchd writes the watcher output.",
    ("prioritize/com.natemccoy.hanadocs-prioritize.plist", "StandardErrorPath /tmp/hanadocs-prioritize/launchd.stderr.log"): "Launchd writes the watcher errors.",
    ("sccache/com.natemccoy.sccache.plist", "StandardOutPath /tmp/sccache-launchd.log"): "Launchd writes the sccache output.",
    ("sccache/com.natemccoy.sccache.plist", "StandardErrorPath /tmp/sccache-launchd.log"): "Launchd writes the sccache errors.",
    ("settings/ensure_git_filters.sh", "StandardErrorPath /tmp/claude-settings-git-refresh.stderr.log"): "Launchd writes the Git filter refresh errors.",
}

# Check each indirect runtime path at its source.
INDIRECT_DESTINATIONS = {
    ('agents/setup.sh', 'LOG_DIR="/tmp/codex-agent-catalog-sync"'): 'The installer prepares the catalog sync log folder.',
    ('claude_to_codex/run_sync.sh', 'LOG_DIR="/tmp/claude-to-codex-sync"'): 'The sync runner shares a lock and a log.',
    ('claude_to_codex/setup.sh', 'LOG_DIR="/tmp/claude-to-codex-sync"'): 'The installer prepares the sync log folder.',
    ('hana_shot/hana_shot.py', 'return RemoteFile(session.remote, f"/tmp/hana_shot_'): 'The remote machine stages the screenshot.',
    ('hooks/post-tool-use-context-usage.py', 'DEBUG_LOG = Path("/tmp/claude/context-hook-debug.jsonl")'): 'The context hook appends debug records.',
    ('lib/session_folder.py', 'FORMER_ROOT = Path("/tmp/claude/session")'): 'The managed session folder root supplies restore paths.',
    ('lib/unit_runs.py', 'RUN_ROOT = Path(os.environ.get("SCRATCH_ROOT") or "/tmp/claude/scratch").parent / "unit"'): 'The managed unit run root supplies the fallback for run folders.',
    ('lib/unit_runs.py', 'ASTRA_RUN_ROOT = Path(os.environ.get("ASTRA_REVIEW_DIR") or "/tmp/claude/astra")'): 'Astra creates review run folders under this runtime root.',
    ('lint/invoke.sh', 'BUILDLOG_PEAK="${TMPDIR:-/tmp}/buildlog.$$.$RANDOM.peak"'): "Writes under the caller's TMPDIR; the fallback is roots.system.",
    ('lint/invoke.sh', 'held="${TMPDIR:-/tmp}/buildlog.$$.$RANDOM$RANDOM.log"'): "Writes under the caller's TMPDIR; the fallback is roots.system.",
    ('lint/invoke.sh', 'LINT_NEXTEST_WIDTH_FILE="${TMPDIR:-/tmp}/nextest_tokens.'): 'The nextest processes share the width file.',
    ('lint/invoke.sh', 'log="${TMPDIR:-/tmp}/lint_invoke.$$.$RANDOM.log"'): "Writes under the caller's TMPDIR; the fallback is roots.system.",
    ('lint/invoke.sh', 'parent="${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}"'): 'The sweep stores the previous run time.',
    ('message/send.py', 'with TemporaryDirectory(prefix="claude-send-", dir="/tmp")'): 'The sender uses a short Unix socket path.',
    ('prioritize/install_watcher.sh', 'STATE_DIR="/tmp/hanadocs-prioritize"'): 'The installer prepares the watcher state folder.',
    ('prioritize/run_watcher.sh', 'STATE_DIR="/tmp/hanadocs-prioritize"'): 'The watcher shares status, a lock, and a log.',
    ('prioritize/writer_lock.py', 'LOCK_PATH = Path("/tmp/hanadocs-prioritize/writer.lock")'): 'The watcher serializes changes with a lock.',
    ('worktree_delete/confirm_gate.sh', 'WT_CONFIRM_DIR="/tmp/claude/worktree_delete"'): 'The worktree commands share a confirmation nonce.',
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

def python_statement_trees(source: str) -> Iterator[tuple[int, ast.AST]]:
    if not PYTHON_PATH_LITERAL.search(source) or not PYTHON_WRITER.search(source):
        return
    # The tokenizer keeps multiline calls and strings in a single statement.
    lines = source.split("\n")
    path_lines = iter(index for index, line in enumerate(lines, 1) if "/tmp" in line)
    path_line = next(path_lines)
    start = 0
    for token in tokenize.generate_tokens(StringIO(source).readline):
        if token.type != tokenize.NEWLINE:
            continue
        end = token.end[0]
        if end < path_line:
            start = end
            continue
        if PYTHON_WRITER.search("\n".join(lines[start:end])):
            while not lines[start].strip() or lines[start].lstrip().startswith("#"):
                start += 1
            statement = "\n".join(lines[start:end]).lstrip(" \t")
            try:
                tree = ast.parse(statement)
            except IndentationError:
                tree = ast.parse(statement + "\n    pass")
            yield start, tree
        start = end
        path_line = next((line for line in path_lines if line > end), 0)
        if not path_line:
            break

def python_writes(source: str) -> Iterator[tuple[int, str]]:
    try:
        trees = list(python_statement_trees(source))
    except (SyntaxError, tokenize.TokenError):
        # Clauses such as elif need the surrounding statement.
        trees = [(0, ast.parse(source))]
    for offset, tree in trees:
        yield from python_tree_writes(tree, offset)

def has_create_flag(flags: ast.AST | None) -> bool:
    if isinstance(flags, ast.BinOp) and isinstance(flags.op, ast.BitOr):
        return has_create_flag(flags.left) or has_create_flag(flags.right)
    return ((isinstance(flags, ast.Attribute) and flags.attr == "O_CREAT")
            or (isinstance(flags, ast.Name) and flags.id == "O_CREAT"))


def python_tree_writes(tree: ast.AST, offset: int) -> Iterator[tuple[int, str]]:
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
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
            target = receiver or python_path(args.get("path", args.get("name", args.get("file", args.get(0)))))
            mode = python_path(args.get("mode", args.get(0 if receiver else 1)))
            if name == "open":
                if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name) and function.value.id == "os":
                    if not has_create_flag(args.get("flags", args.get(1))):
                        continue
                elif not re.search("[wax]", mode):
                    continue
        if TEMP_PATH.match(target):
            yield offset + call.lineno, f"{name} {target}"

def writes(filename: str, source: str) -> Iterator[tuple[int, str]]:
    if filename.endswith(".py"):
        yield from python_writes(source)
    if filename.endswith((".sh", ".zsh")) or re.match(r"#![^\n]*\b(?:ba|da|z|k)?sh\b", source):
        for match in SHELL_WRITE.finditer(source.replace("\\\n", " \v")):
            line = source.count("\n", 0, match.start()) + 1
            if inline := match["substitution"] or match["header"]:
                yield from ((line, target) for _, target in writes(filename, inline))
            operands = match["operands"] or match["redirect"] or ""
            if match["tool"] == "install" and not re.search(r"(?:^|\s)(?:-d|--directory)(?:\s|$)", operands):
                continue
            for word in re.finditer(SHELL_WORD, operands):
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
                           'Path("/tmp/note").write_text("hello")', 'open("/tmp/file", "w")',
                           'os.open(path="/tmp/file", flags=os.O_WRONLY | os.O_CREAT)'],
                          ['path = Path("/tmp/work")', 'assert Path("/tmp/work").exists()',
                           '# Path("/tmp/work").mkdir()', '"""os.mkdir("/tmp/work")"""', 'open("/tmp/file", "r")',
                           'output.write_text("mkdir /tmp/work")', '(Path("/tmp") / "/managed/file").touch()',
                           'os.open("/tmp/input", os.O_RDONLY)']),
            "sample.sh": (['mkdir -p "/tmp/work"', 'x=$(mktemp "${TMPDIR:-/tmp}/x.XXXXXX")',
                           'touch primary /private/tmp/stamp', 'printf hi >> /tmp/out', 'install -m 755 -d /tmp/dir',
                           'x="$(mktemp "/tmp/x.XXXXXX")"', "cat <<'EOF' > /tmp/out\ndata\nEOF"],
                          ['echo "mkdir /tmp/work"', 'echo "> /tmp/out"', '# mkdir /tmp/work',
                           'input=/tmp/work', 'cat < /tmp/in', 'echo mkdir /tmp/work', 'install /tmp/source ./target',
                           '[[ "$value" > /tmp/data ]]', 'mkdir relative # /tmp/data', "cat <<'EOF'\ntouch /tmp/data\nEOF"]),
            "sample.plist": (['<key>StandardOutPath</key>\n<string>/tmp/out</string>',
                              '<key>StandardErrorPath</key><string>/private/tmp/err</string>'],
                             ['<key>WorkingDirectory</key><string>/tmp/work</string>']),
        }
        for filename, (creation, data) in samples.items():
            for source in creation + data:
                self.assertEqual(bool(list(writes(filename, source))), source in creation, source)

    def test_scripts_use_approved_temporary_paths(self) -> None:
        command = ["git", "grep", "-l", "-z", "-e", "/tmp", "--", "scripts"]
        result = subprocess.run(command, cwd=ROOT, stdout=subprocess.PIPE, check=False)
        if result.returncode not in (0, 1):
            result.check_returncode()
        allowed = set(ALLOWED_DESTINATIONS)
        indirect = set(INDIRECT_DESTINATIONS)
        problems: list[str] = []
        for filename in (os.fsdecode(name) for name in result.stdout.split(b"\0") if name):
            relative = filename.removeprefix("scripts/")
            source = (ROOT / filename).read_text(encoding="utf-8", errors="surrogateescape")
            for key in INDIRECT_DESTINATIONS:
                if key[0] == relative and any(line.lstrip().startswith(key[1]) for line in source.splitlines()):
                    indirect.discard(key)
            for line, destination in writes(filename, source):
                key = (relative, destination)
                if key not in allowed:
                    problems.append(f"{filename}:{line}: Unapproved destination {destination}")
                allowed.discard(key)
        for filename, text in sorted(allowed | indirect):
            problems.append(f"scripts/{filename}: Stale allowlist entry {text}")
        self.assertFalse(problems, "\n" + "\n".join(problems))
