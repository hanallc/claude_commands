"""Private settings for the Hana guest image."""
from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from functools import partial
from pathlib import Path
import shutil
from unittest import mock

from scripts.aws_target import hana_render
from scripts.lib.build_folders_test_support import temporary_folder_settings
from scripts.lint.memory_test_support import write_config_fixture


def stage_runtime_sources(checkout: Path) -> list[str]:
    """Copy the Hana runtime manifest and its source files."""
    manifest = hana_render.RUNTIME_FILE_LIST
    source = hana_render.CHECKOUT
    entries = (source / manifest).read_text(encoding="utf-8").splitlines()
    for relative in (manifest, *map(Path, entries)):
        if not relative.is_relative_to("scripts") or ".." in relative.parts:
            raise ValueError(f"Invalid Hana runtime path: {relative}")
        destination = checkout / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        _ = shutil.copyfile(source / relative, destination)
    return entries


@contextmanager
def guest_digest_fixture(root: Path) -> Generator[None]:
    """Compute guest digests from a private checkout."""
    checkout = root / "guest-image"
    _ = write_config_fixture(checkout / "config/build_targets.conf",
                             defaults="# range: 1..\nloopback.command_timeout_s=23\n")
    _ = write_config_fixture(checkout / "config/build_folders.conf", defaults=temporary_folder_settings(checkout))
    manifest = checkout / hana_render.IMAGE_FILE_LIST
    manifest.parent.mkdir(parents=True)
    _ = manifest.write_text("scripts/fixture.py\n")
    _ = (checkout / "scripts/fixture.py").write_text("fixture = True\n")
    digest = partial(hana_render.guest_digest, checkout=checkout)
    with mock.patch.object(hana_render, "guest_digest", new=digest):
        yield
