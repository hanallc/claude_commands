"""Hash the admission sources without importing admission code."""
from __future__ import annotations

from collections.abc import Iterable
import hashlib
from pathlib import Path
import sys


ADMISSION_CODE_PATHS = (
    "direct/verify.sh", "lint/invoke.sh", "hooks/pre-tool-use-brp-launch-gate.sh",
    "production/gate.py", "production/targeted_test.py", "lint/memory_gate.sh", "lint/memory_admit.py",
    "build_queue/admission_code.py", "build_queue/admission_client.py", "build_queue/admission_protocol.py", "build_queue/admission_wire.py",
    "build_queue/admission_jobs.py",
    "build_queue/system_clock.py", "build_queue/admission_records.py", "build_queue/step_estimator.py",
    "build_queue/admission_service.py", "build_queue/queue_place.py", "build_queue/placement.py",
    "build_queue/execution_identity.py",
    "aws_target/hana_placement.py", "aws_target/hana_placement_adapter.py", "aws_target/hana_box.py", "aws_target/hana_spend.py",
    "aws_target/hana_execution.py", "build_targets/target_config.py", "build_targets/build_targets.py",
    "build_targets/execution_transport.py",
    "build_queue/scope_activity.py", "build_queue/queue_snapshot.py", "build_queue/progress_profile.py", "build_queue/snapshot_codec.py",
    "lib/suite_runners.py", "lib/build_steps.py", "lib/boot_identity.py", "lib/inotify.py", "lib/conf.py", "lib/atomic_file.py", "lib/test_phase.py",
    "lint/build_shape.py", "buildlog/store.py", "buildlog/record.py", "buildlog/repository.py", "buildlog/parse.py",
)


RELOAD_SOURCE_SUFFIX = ".py"


def admission_code_version(scripts: Path | None = None) -> str:
    root = scripts if scripts is not None else Path(__file__).resolve().parents[1]
    return _source_hash(root, ADMISSION_CODE_PATHS)


def reload_source_version(root: Path) -> str:
    """Hash the Python source files that the watch can see."""
    paths = sorted(str(path.relative_to(root)) for path in root.rglob(f"*{RELOAD_SOURCE_SUFFIX}") if path.is_file())
    return _source_hash(root, paths)


def _source_hash(root: Path, paths: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for relative in paths:
        try:
            content = (root / relative).read_bytes()
        except OSError:
            return ""
        digest.update(relative.encode() + b"\0" + content + b"\0")
    return digest.hexdigest()


if __name__ == "__main__":
    version = admission_code_version()
    if not version:
        sys.exit("admission code is unavailable")
    print(version)
