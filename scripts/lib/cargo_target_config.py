"""Read Cargo folder settings without loading a build or sweep caller."""

from __future__ import annotations

import os
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import cast


class CargoConfigPolicy(Enum):
    COMPATIBLE = auto()
    CURRENT_STRICT = auto()


@dataclass(frozen=True)
class CargoFolderSetting:
    value: str
    base: Path


def cargo_path_argument(argv: list[str], flag: str) -> str:
    for index, arg in enumerate(argv):
        if arg.startswith(flag + "="):
            return arg.split("=", 1)[1]
        if arg == flag and index + 1 < len(argv):
            return argv[index + 1]
    return ""


def cargo_config_values(argv: list[str]) -> list[str]:
    """Read each nonempty Cargo config argument in command order."""
    values: list[str] = []
    for index, arg in enumerate(argv):
        value = arg.split("=", 1)[1] if arg.startswith("--config=") else (
            argv[index + 1] if arg == "--config" and index + 1 < len(argv) else "")
        if value:
            values.append(value)
    return values


def cargo_home_folder(cwd: str, environment: Mapping[str, str]) -> str:
    """Resolve Cargo home against the command's working folder."""
    cargo_home = environment.get("CARGO_HOME")
    if not cargo_home and environment.get("HOME"):
        cargo_home = os.path.join(environment["HOME"], ".cargo")
    return os.path.realpath(os.path.join(cwd, cargo_home)) if cargo_home else ""


def cargo_config_folders(cwd: str, environment: Mapping[str, str]) -> list[Path]:
    """List Cargo home and the ancestor config folders in precedence order."""
    cargo_home = cargo_home_folder(cwd, environment)
    folders = [Path(cargo_home)] if cargo_home else []
    directory = Path(cwd)
    folders.extend(parent / ".cargo" for parent in reversed((directory, *directory.parents)))
    return folders


def cargo_folder_settings(argv: list[str], cwd: str, environment: Mapping[str, str], *,
                          policy: CargoConfigPolicy = CargoConfigPolicy.COMPATIBLE) -> dict[str, CargoFolderSetting]:
    """Apply Cargo precedence to folder settings under both policies.

    Read config files, then CARGO_BUILD_TARGET_DIR and CARGO_BUILD_BUILD_DIR, then CLI config.
    Apply CARGO_TARGET_DIR next, then the first --target-dir argument.
    Strict mode reads current config files and raises for invalid or unreadable CLI config.
    """
    strict = policy is CargoConfigPolicy.CURRENT_STRICT
    directory = Path(cwd)
    settings: dict[str, CargoFolderSetting] = {}

    def read_settings(contents: str, base: Path) -> None:
        try:
            table = cast(dict[str, object], tomllib.loads(contents)).get("build")
        except tomllib.TOMLDecodeError:
            if strict:
                raise
            return
        if isinstance(table, dict):
            for key in ("target-dir", "build-dir"):
                value = cast(dict[str, object], table).get(key)
                if isinstance(value, str) and value:
                    settings[key] = CargoFolderSetting(value, base)

    def read_config(path: Path) -> None:
        try:
            read_settings(path.read_text(), path.parent.parent)
        except FileNotFoundError:
            pass
        except (OSError, UnicodeError):
            if strict:
                raise

    for config in cargo_config_folders(cwd, environment):
        legacy = config / "config"
        read_config(legacy if not strict and legacy.is_file() else config / "config.toml")
    for key, variable in (("target-dir", "CARGO_BUILD_TARGET_DIR"), ("build-dir", "CARGO_BUILD_BUILD_DIR")):
        if environment.get(variable):
            settings[key] = CargoFolderSetting(environment[variable], directory)
    for setting in cargo_config_values(argv):
        if "=" in setting:
            read_settings(setting, directory)
        elif strict:
            path = directory / setting
            read_settings(path.read_text(), path.parent.parent)
        else:
            read_config(directory / setting)
    if environment.get("CARGO_TARGET_DIR"):
        settings["target-dir"] = CargoFolderSetting(environment["CARGO_TARGET_DIR"], directory)
    named = cargo_path_argument(argv, "--target-dir")
    if named:
        settings["target-dir"] = CargoFolderSetting(named, directory)
    return settings


def cargo_workspace_manifest(argv: list[str], cwd: str, cargo_home: str = "") -> Path:
    """Find the workspace manifest without starting Cargo or reading its cache.

    Cargo core/workspace.rs at 58bab51c: find_root_iter and is_excluded.
    """
    directory = Path(cwd)
    named = cargo_path_argument(argv, "--manifest-path")
    if named:
        manifest = Path(os.path.abspath(directory / named))
    else:
        manifest = next((parent / "Cargo.toml" for parent in (directory, *directory.parents)
                         if (parent / "Cargo.toml").is_file()), directory / "Cargo.toml")
    table = cast(dict[str, object], tomllib.loads(manifest.read_text()))
    if isinstance(table.get("workspace"), dict):
        return manifest
    package = table.get("package")
    if isinstance(package, dict):
        workspace = cast(dict[str, object], package).get("workspace")
        if isinstance(workspace, str):
            root = manifest.parent / workspace / "Cargo.toml"
            root_table = cast(dict[str, object], tomllib.loads(root.read_text()))
            if isinstance(root_table.get("workspace"), dict):
                return root
            raise ValueError("Cargo package.workspace does not name a workspace")
    for parent in manifest.parent.parents:
        if parent.parts[-2:] == ("target", "package"):
            break
        if cargo_home and manifest.is_relative_to(cargo_home) and not parent.is_relative_to(cargo_home):
            break
        root = parent / "Cargo.toml"
        if not root.is_file():
            continue
        table = cast(dict[str, object], tomllib.loads(root.read_text()))
        workspace = table.get("workspace")
        if isinstance(workspace, dict):
            matches: dict[str, bool] = {}
            for key in ("exclude", "members"):
                paths = cast(dict[str, object], workspace).get(key, [])
                if not isinstance(paths, list) or any(not isinstance(path, str) for path in cast(list[object], paths)):
                    raise ValueError("Cargo workspace membership cannot be read")
                matches[key] = any(manifest.is_relative_to(parent / path) for path in cast(list[str], paths))
            if matches["exclude"] and not matches["members"]:
                continue
            return root
        package = table.get("package")
        if isinstance(package, dict) and "workspace" in package:
            raise ValueError("An ancestor Cargo manifest redirects workspace discovery")
    return manifest


def cargo_short_hash(data: bytes) -> str:
    """Cargo's StableSipHasher128, reduced and written as little-endian hex.

    Sources: Cargo src/cargo/util/{hasher,hex}.rs at 58bab51c and
    https://github.com/rust-lang/rustc-stable-hash/blob/main/src/sip128.rs
    """
    mask = (1 << 64) - 1
    v0, v1, v2, v3 = 0x736f6d6570736575, 0x646f72616e646f83, 0x6c7967656e657261, 0x7465646279746573

    def rotate(value: int, bits: int) -> int:
        return ((value << bits) | (value >> (64 - bits))) & mask

    def rounds(count: int) -> None:
        nonlocal v0, v1, v2, v3
        for _ in range(count):
            v0 = (v0 + v1) & mask
            v2 = (v2 + v3) & mask
            v1 = rotate(v1, 13) ^ v0
            v3 = rotate(v3, 16) ^ v2
            v0 = rotate(v0, 32)
            v2 = (v2 + v1) & mask
            v0 = (v0 + v3) & mask
            v1 = rotate(v1, 17) ^ v2
            v3 = rotate(v3, 21) ^ v0
            v2 = rotate(v2, 32)

    end = len(data) - len(data) % 8
    for offset in range(0, end, 8):
        word = int.from_bytes(data[offset:offset + 8], "little")
        v3 ^= word
        rounds(1)
        v0 ^= word
    tail = int.from_bytes(data[end:], "little") | ((len(data) & 0xff) << 56)
    v3 ^= tail
    rounds(1)
    v0 ^= tail
    v2 ^= 0xee
    rounds(3)
    low = v0 ^ v1 ^ v2 ^ v3
    v1 ^= 0xdd
    rounds(3)
    high = v0 ^ v1 ^ v2 ^ v3
    return ((low * 3 + high) & mask).to_bytes(8, "little").hex()


def cargo_workspace_path_hash(manifest: Path) -> str:
    """Cargo hashes the canonical workspace manifest, then splits first two/rest.

    https://rust.googlesource.com/cargo/+/58bab51ca444e109a21ae9e0f17c27e38173a660/src/cargo/util/context/mod.rs
    Path's component bytes and boundary bits follow Rust's Unix Hash for Path:
    https://github.com/rust-lang/rust/blob/1.91.0/library/std/src/path.rs
    """
    if os.name != "posix" or sys.maxsize != (1 << 63) - 1:
        raise ValueError("Cargo workspace path hashing is unavailable on this platform")
    try:
        path = manifest.resolve(strict=True)
    except OSError:
        path = manifest
    if not path.is_absolute():
        raise ValueError("Cargo workspace manifest path is not absolute")
    data = bytearray()
    boundaries = 0
    for component in os.fsencode(path).split(b"/"):
        if component:
            data.extend(component)
            boundaries = (boundaries + len(component)) & ((1 << 64) - 1)
            boundaries = (boundaries >> 2) | ((boundaries & 3) << 62)
    data.extend(boundaries.to_bytes(8, "little"))
    hashed = cargo_short_hash(bytes(data))
    return os.path.join(hashed[:2], hashed[2:])


def cargo_build_folder(setting: CargoFolderSetting, argv: list[str], cwd: str, cargo_home: str) -> str:
    """Expand build.build-dir before resolving it against its configuration root.

    https://doc.rust-lang.org/cargo/reference/config.html#buildbuild-dir
    Unreadable workspace state and unknown templates must keep all caches busy.
    """
    value = setting.value
    replacements: dict[str, str] = {}
    if "{workspace-root}" in value or "{workspace-path-hash}" in value:
        manifest = cargo_workspace_manifest(argv, cwd, cargo_home)
        replacements["workspace-root"] = str(manifest.parent)
        if "{workspace-path-hash}" in value:
            replacements["workspace-path-hash"] = cargo_workspace_path_hash(manifest)
    if cargo_home:
        replacements["cargo-cache-home"] = os.path.realpath(os.path.join(cwd, cargo_home))
    for name in ("workspace-root", "cargo-cache-home", "workspace-path-hash"):
        value = value.replace("{" + name + "}", replacements.get(name, "{" + name + "}"))
    if "{" in value or "}" in value:
        raise ValueError("Cargo build folder template cannot be resolved")
    return os.path.realpath(setting.base / value)
