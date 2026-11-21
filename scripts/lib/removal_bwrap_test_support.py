"""Run removal commands against private test data."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import sysconfig

from scripts.lib.build_folders_test_support import workspace_folder_settings
from scripts.lib.scratch_test_support import write_scratch_settings
from scripts.lint.config_test_support import FOLDER_AUDIT_SETTINGS, SWEEP_SCAN_SETTINGS, TMP_SWEEP_SETTINGS
from scripts.lint.memory_test_support import write_config_fixture
from scripts.mac_test.config_test_support import MAC_OFFLOAD_SETTINGS

LINUX_PRIVATE_PATHS = tuple(Path(name) for name in ("/home", "/tmp", "/build", "/session", "/dev/shm", "/var/tmp"))
LINUX_CHECKOUT = Path("/home/removal-source")
MAC_BACKEND = Path("/usr/bin/sandbox-exec")


def mac_profile(private: Path, checkout: Path) -> str:
    """Permit fixture data and the code and runtime that the proof runs."""
    def subpath(path: Path) -> str:
        return f"(subpath {json.dumps(str(path), ensure_ascii=False)})"

    interpreter = Path(sys.executable).resolve()
    libraries = {Path(sysconfig.get_path(name)).resolve() for name in ("stdlib", "platstdlib")}
    python_library = (Path(sys.base_prefix) / "Python").resolve()
    runtime = tuple(Path(path) for path in (
        "/bin", "/usr/bin", "/usr/lib", "/System/Library", "/usr/share/zoneinfo", "/private/var/db/timezone",
    ))
    readable = (private, checkout, *sorted(libraries), *runtime)
    readable_files = (interpreter, python_library, Path("/dev/null"))
    parents = {parent for path in (*readable, *readable_files) for parent in path.parents}
    directories = " ".join(map(subpath, readable))
    def literals(paths: tuple[Path, ...] | list[Path]) -> str:
        return " ".join(f"(literal {json.dumps(str(path), ensure_ascii=False)})" for path in paths)

    cache = f"dataclass-code-{os.getuid()}"
    return (
        "(version 1)\n(allow default)\n(deny network*)\n(deny file-read*)\n(deny file-write*)\n"
        f"(allow file-read* {directories} {literals(readable_files)})\n"
        f"(allow file-read-metadata {literals(sorted(parents))})\n"
        f"(deny file-read* (subpath \"/var/tmp/{cache}\") (subpath \"/private/var/tmp/{cache}\"))\n"
        f"(allow file-write* {subpath(private)} (literal \"/dev/null\"))\n"
    )


class BuildMount(Enum):
    REGULAR = "use the private build folder"
    BIND_UNIT = "bind the private source over the unit"


@dataclass(frozen=True)
class RemovalSandbox:
    """Use the platform sandbox for one removal proof."""

    fixture: Path
    mount: BuildMount = BuildMount.REGULAR

    @property
    def checkout(self) -> Path:
        return Path(__file__).resolve().parents[2]

    @property
    def home(self) -> Path:
        return self.fixture / "home" if sys.platform == "darwin" else Path("/home/removal-proof")

    @property
    def temporary(self) -> Path:
        return self.fixture / "tmp" if sys.platform == "darwin" else Path("/tmp")

    @property
    def build(self) -> Path:
        return self.home / "build" if sys.platform == "darwin" else Path("/build")

    @property
    def outside(self) -> Path:
        return self.fixture.parent / "outside"

    @property
    def bind_source(self) -> Path:
        return self.fixture / "bind-source"

    def host_path(self, path: Path) -> Path:
        roots = (self.fixture,) if sys.platform == "darwin" else LINUX_PRIVATE_PATHS
        if (not path.is_absolute() or ".." in path.parts or not any(path.is_relative_to(root) for root in roots)
                or sys.platform == "linux" and path.is_relative_to(LINUX_CHECKOUT)):
            raise AssertionError(f"removal proof has no private folder for {path}")
        local = path if sys.platform == "darwin" else self.fixture / path.relative_to("/")
        if not local.resolve().is_relative_to(self.fixture):
            raise AssertionError(f"the removal proof path leaves the fixture: {path}")
        return local

    def contains(self, path: Path) -> bool:
        try:
            _ = self.host_path(path)
        except (AssertionError, OSError, RuntimeError):
            return False
        return True

    def configure(self) -> None:
        """Write the private roots and staff record."""
        for path in (self.build, self.temporary, self.home / ".claude" / "sessions"):
            self.host_path(path).mkdir(parents=True, exist_ok=True)
        config = self.host_path(self.home / "config")
        _ = write_config_fixture(config / "build_folders.conf", f"""\
roots.system={self.temporary}
roots.temporary={self.temporary}
removal.forms={self.build}/<repo>/<unit>/<short>
""", defaults=workspace_folder_settings(self.home))
        _ = write_config_fixture(config / "lint.conf",
                                 defaults=FOLDER_AUDIT_SETTINGS + SWEEP_SCAN_SETTINGS + TMP_SWEEP_SETTINGS)
        write_scratch_settings(config, self.home)
        if sys.platform == "darwin":
            _ = write_config_fixture(config / "mac_test.conf", defaults=MAC_OFFLOAD_SETTINGS)
            self.configure_mac()
        else:
            (self.fixture / LINUX_CHECKOUT.relative_to("/")).mkdir()
        producer = "11111111-1111-4111-8111-111111111111"
        showrunner = "33333333-3333-4333-8333-333333333333"
        unit = "22222222-2222-4222-8222-222222222222"
        _ = (self.host_path(self.home) / "staff.json").write_text(json.dumps({"schema": 2, "rows": [
            {"id": producer, "parent": None, "kind": "producer", "name": "proof",
             "worktree": {"machine": "natedev", "path": str(self.home / "rust" / "proof"), "state": "present"}},
            {"id": showrunner, "parent": None, "kind": "showrunner", "name": "proof"},
            {"id": unit, "parent": showrunner, "kind": "unit", "name": "unit", "unit_type": "direct"},
        ]}))
        _ = self.outside.write_text("outside the removal fixture\n")

    def configure_mac(self) -> None:
        fixture_bin = self.fixture / "bin"
        fixture_bin.mkdir()
        commands = {
            "ssh": r"""#!/bin/sh
case "$*" in
    *floor_claims.py*) printf '%s\n' '{"repositories":[],"mirrors":[]}' ;;
    *) exit 1 ;;
esac
""",
            "lsof": "#!/bin/sh\nprintf 'p1\\nfcwd\\nn/\\n'\n",
            "ps": r"""#!/bin/sh
[ "$*" = '-axww -o pid=,ppid=,args=' ] || exit 1
printf '1 0 launchd\n%s 1 %s\n' "$PPID" "$REMOVAL_PROOF_CALLER_ARGS"
""",
        }
        for name, script in commands.items():
            path = fixture_bin / name
            _ = path.write_text(script)
            path.chmod(0o700)
        _ = (self.fixture / "removal.sb").write_text(mac_profile(self.fixture, self.checkout))

    def python(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        """Run one Python command in the platform sandbox."""
        return self.run(str(Path(sys.executable).resolve()), "-B", *arguments)

    def script(self, script: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        """Run the installed script command with the checkout copy."""
        checkout = self.checkout if sys.platform == "darwin" else LINUX_CHECKOUT
        return self.run(str(checkout / "scripts/lib/py"), str(checkout / script), *arguments)

    def run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        env = {
            "HOME": str(self.home), "CONF_DIR": str(self.home / "config"), "TMPDIR": str(self.temporary),
            "STAFF_FILE": str(self.home / "staff.json"), "BUILD_HOLD_DIR": str(self.home / "holds"),
            "PATH": "/run/current-system/sw/bin:/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C.UTF-8",
            "CLAUDE_PYTHON": str(Path(sys.executable).resolve()), "USER_ZONE": "UTC",
            "PYTHONDONTWRITEBYTECODE": "1", "LINT_SWEEP_BUDGET_GIB": "24",
            "CLAUDE_DISABLE_DATACLASS_CODE_CACHE": "1",
            "LINT_SWEEP_STATE_DIR": str(self.home / "sweep"), "BUILDLOG_DIR": str(self.home / "buildlog"),
            "SCRATCH_ROOT": str(self.home / "scratch"), "SCRATCH_COPIES_ROOT": str(self.home / "copies"),
            "NOTIFIER_SESSIONS_DIR": str(self.home / ".claude/sessions"),
        }
        if sys.platform == "darwin":
            if not os.access(MAC_BACKEND, os.X_OK):
                raise AssertionError(f"removal proof requires {MAC_BACKEND}")
            profile = self.fixture / "removal.sb"
            env["PATH"] = f"{self.fixture / 'bin'}:{env['PATH']}"
            env["REMOVAL_PROOF_CALLER_ARGS"] = " ".join(arguments)
            return subprocess.run([str(MAC_BACKEND), "-f", str(profile), *arguments], cwd=self.fixture,
                                  env=env, text=True, capture_output=True, timeout=90, check=False)
        if sys.platform != "linux":
            raise AssertionError(f"removal proof has no sandbox backend for {sys.platform}")
        bubblewrap = shutil.which("bwrap")
        if bubblewrap is None:
            raise AssertionError("removal proof requires bwrap on PATH")
        command = [bubblewrap, "--unshare-all", "--die-with-parent", "--ro-bind", "/", "/",
                   "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/run/user",
                   "--hostname", "natedev", "--clearenv"]
        for location in LINUX_PRIVATE_PATHS:
            folder = self.host_path(location)
            folder.mkdir(parents=True, exist_ok=True)
            command.extend(("--bind", str(folder), str(location)))
        if self.mount is BuildMount.BIND_UNIT:
            command.extend(("--bind", str(self.bind_source), str(self.build / "proof/unit")))
        command.extend(("--ro-bind", str(self.checkout), str(LINUX_CHECKOUT), "--chdir", str(self.home)))
        for key, value in env.items():
            command.extend(("--setenv", key, value))
        return subprocess.run([*command, *arguments], text=True, capture_output=True, timeout=90, check=False)
