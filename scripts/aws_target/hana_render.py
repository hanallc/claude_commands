"""Render the Hana image and per-run files from repository inputs."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import cast

from scripts.aws_target import hana_guest, hana_receiver
from scripts.lib import conf


CHECKOUT = Path(__file__).resolve().parents[2]
DATA_DEVICE = Path('/dev/sdf')
HANA_TARGET_ALIAS_KEY = 'host'
HANA_IMAGE_KEYS = (
    'enabled', 'guard_grace_seconds', 'watchdog_check_seconds', 'permit_max_seconds',
    'step_cpu_limit', 'step_memory_gib', 'mount_timeout_seconds', 'aws_timeout_s', 'control_poll_seconds',
    'result_transfer_timeout_s', 'source_max_bytes', 'source_download_timeout_s',
)
HANA_RUNTIME_KEYS = ('execution_shell', 'free_floor_gib', 'source_git_timeout_s', 'source_allowed_environment')
COMMAND_TIMEOUT_KEY = 'loopback.command_timeout_s'
SHELL_TIMEOUT_KEY = 'loopback.shell_timeout_s'
SLOT_PROBE_KEYS = ('locks.probe_wait_seconds', 'locks.probe_retry_seconds')
IMAGE_SETTINGS = {
    'hana_aws': HANA_IMAGE_KEYS,
    'build_targets': (COMMAND_TIMEOUT_KEY,),
    'build_folders': SLOT_PROBE_KEYS,
}
IMAGE_FILE_LIST = Path('scripts/aws_target/hana_image.files')
RUNTIME_FILE_LIST = Path('scripts/aws_target/hana_render.files')
RUNNER_ENTRY = Path('scripts/aws_target/hana_runner_entry.py')


@dataclass(frozen=True)
class GuestDigest:
    value: str


@dataclass(frozen=True)
class GuestDigestUnknown:
    """The artifact or deployment has no verified guest digest."""


def decode_guest_digest(value: object) -> GuestDigest | GuestDigestUnknown:
    return GuestDigest(value) if isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) else GuestDigestUnknown()


@dataclass(frozen=True)
class RenderedGuestFile:
    path: Path
    mode: int
    content: bytes


class RenderArguments(argparse.Namespace):
    device: Path = Path()
    out_dir: Path = Path()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument('--device', type=Path, required=True)
    _ = parser.add_argument('out_dir', type=Path)
    arguments = parser.parse_args(argv, namespace=RenderArguments())
    files = guest_files(CHECKOUT, CHECKOUT / 'config/hana_aws.conf', arguments.device)
    digest = digest_files(files)
    output = arguments.out_dir
    for file in files:
        destination = output / file.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        content = file.content
        if file.path == Path('safety.json'):
            safety = cast(dict[str, object], json.loads(content))
            content = (json.dumps({**safety, 'guest_digest': digest.value}) + '\n').encode()
        _ = destination.write_bytes(content)
        destination.chmod(file.mode)
    return 0


def affects_guest_config(key: str, configuration: Path) -> bool:
    """Check whether the key selects an image setting for the Hana alias."""
    stem = configuration.stem
    if stem == 'hana_aws' and key == HANA_TARGET_ALIAS_KEY:
        return True
    keys = IMAGE_SETTINGS.get(stem, ())
    if not any(key == base or key.startswith(base + '.') for base in keys):
        return False
    settings = conf.settings(configuration.read_text(encoding='utf-8'))
    hana_settings = settings if stem == 'hana_aws' else conf.settings(
        configuration.with_name('hana_aws.conf').read_text(encoding='utf-8'))
    alias = guest_host_name(hana_settings)
    return any(conf.effective_key(settings, base, host_name=alias) == key for base in keys)


def guest_host_name(settings: dict[str, conf.Setting]) -> str:
    # Select overrides by the Hana target alias.
    host = settings.get(HANA_TARGET_ALIAS_KEY)
    host_name = host.value.partition('.')[0] if host is not None else ''
    if not host_name:
        raise ValueError('The Hana guest host is missing.')
    return host_name


def resolved_config_text(settings: dict[str, conf.Setting], keys: tuple[str, ...], host_name: str) -> str:
    selected: dict[str, conf.Setting] = {}
    for key in keys:
        setting = settings.get(conf.effective_key(settings, key, host_name=host_name))
        if setting is None:
            raise ValueError(f'The Hana guest setting {key} is missing.')
        selected[key] = setting
    return config_text(selected)


def config_text(settings: dict[str, conf.Setting]) -> str:
    rows: list[str] = []
    for key, setting in settings.items():
        if setting.limit.startswith(('# whole:', '# range:', '# one of:')):
            rows.append(setting.limit)
        rows.append(f'{key}={setting.value}')
    return '\n'.join(rows) + '\n'


def _runtime_defaults(settings: dict[str, conf.Setting], alias: str) -> dict[str, conf.Setting]:
    rows: dict[str, conf.Setting] = {}
    for key in settings:
        base, _, suffix = key.rpartition('.')
        if base and suffix == alias:
            rows[base] = settings[key]
        elif base not in settings:
            _ = rows.setdefault(key, settings[key])
    return rows


def _workload(configuration: Path | conf.ConfigRead) -> tuple[conf.ConfigRead, dict[str, conf.Setting]]:
    workload = conf.read(configuration) if isinstance(configuration, Path) else configuration
    settings = workload.settings
    if not isinstance(settings, dict):
        raise ValueError(f'{workload.source}: {settings.reason}')
    return workload, settings


def _resolved_read(path: Path, text: str, stamp: conf.ConfigRead) -> conf.ConfigRead:
    return conf.ConfigRead(path, path, conf.settings(text), False, stamp.stamp)


def _listed_files(checkout: Path, manifest: Path) -> tuple[RenderedGuestFile, ...]:
    root = checkout.resolve()
    files: dict[Path, RenderedGuestFile] = {}
    for entry in (root / manifest).read_text(encoding='utf-8').splitlines():
        relative = Path(entry)
        source = root / relative
        if (not relative.parts or relative.is_absolute() or '..' in relative.parts or relative in files
                or relative.parts[0] != 'scripts' or relative.suffix != '.py'
                or source.resolve() != source or not source.is_file()):
            raise ValueError(f'{manifest}: invalid file path {entry!r}')
        files[relative] = RenderedGuestFile(relative, 0o644, source.read_bytes())
    return tuple(files.values())


def guest_files(checkout: Path, configuration: Path | conf.ConfigRead, device: Path) -> tuple[RenderedGuestFile, ...]:
    """Render the controls and the installed image closure."""
    workload, settings = _workload(configuration)
    alias = guest_host_name(settings)
    image_config = resolved_config_text(settings, HANA_IMAGE_KEYS, alias)
    parsed = _resolved_read(workload.source, image_config, workload)
    targets = checkout / 'config/build_targets.conf'
    command_config = resolved_config_text(conf.settings(targets.read_text(encoding='utf-8')),
                                          (COMMAND_TIMEOUT_KEY,), alias)
    command_timeout = _resolved_read(targets, command_config, workload).value(
        COMMAND_TIMEOUT_KEY, float, recover_invalid=False)
    folders = checkout / 'config/build_folders.conf'
    slot_config = resolved_config_text(conf.settings(folders.read_text(encoding='utf-8')), SLOT_PROBE_KEYS, alias)
    slot_policy = _resolved_read(folders, slot_config, workload)
    wait_key, retry_key = SLOT_PROBE_KEYS
    wait = slot_policy.value(wait_key, conf.decimal, recover_invalid=False)
    retry = slot_policy.value(retry_key, conf.decimal, recover_invalid=False)
    if wait < 0:
        raise ValueError(f'{folders}: {wait_key} must be nonnegative.')
    if float(retry) <= 0:
        raise ValueError(f'{folders}: {retry_key} must be positive.')
    control = (*hana_guest.render_files(hana_guest.read_settings(parsed, recover_invalid=False), device=device),
               *hana_receiver.render_files(hana_receiver.read_settings(parsed, command_timeout_s=command_timeout)))
    return (
        *(RenderedGuestFile(Path(file.path.name), file.mode, file.content.encode()) for file in control),
        *(RenderedGuestFile(Path('runtime') / file.path, file.mode, file.content)
          for file in _listed_files(checkout, IMAGE_FILE_LIST)),
        RenderedGuestFile(Path('runtime/config/build_folders.conf'), 0o644, slot_config.encode()),
    )


def runtime_files(checkout: Path, configuration: Path | conf.ConfigRead) -> tuple[RenderedGuestFile, ...]:
    """Render the run files with paths relative to the archive's runtime folder."""
    _, settings = _workload(configuration)
    alias = guest_host_name(settings)
    runtime_config = resolved_config_text(settings, HANA_RUNTIME_KEYS, alias)
    targets = checkout / 'config/build_targets.conf'
    shell_config = resolved_config_text(conf.settings(targets.read_text(encoding='utf-8')),
                                        (SHELL_TIMEOUT_KEY,), alias)
    copies = conf.settings((checkout / 'config/copies.conf').read_text(encoding='utf-8'))
    copies_config = config_text(_runtime_defaults({key: setting for key, setting in copies.items()
        if key != 'root' and key.rpartition('.')[0] != 'root'}, alias))
    folders = conf.settings((checkout / 'config/build_folders.conf').read_text(encoding='utf-8'))
    # The image supplies the slot pool and lock timing.
    image_roots = ('roots.slots', 'roots.slot_cargo', 'roots.mirror', 'roots.shell', 'roots.temporary')
    folder_defaults = _runtime_defaults(folders, alias)
    folder_config = config_text({key: setting for key, setting in folder_defaults.items()
        if not any(key == base or key.startswith(base + '.') for base in (*image_roots, *SLOT_PROBE_KEYS))
        and (not key.startswith('slots.') or key in ('slots.unused_days', 'slots.wait_seconds'))})
    files = {file.path: file for file in _listed_files(checkout, RUNTIME_FILE_LIST)}
    entry = files.pop(RUNNER_ENTRY)
    return (
        *files.values(),
        replace(entry, path=Path('runner.py')),
        RenderedGuestFile(Path('config/hana_aws.conf'), 0o644, runtime_config.encode()),
        RenderedGuestFile(Path('config/build_targets.conf'), 0o644, shell_config.encode()),
        RenderedGuestFile(Path('config/copies.conf'), 0o644, copies_config.encode()),
        RenderedGuestFile(Path('config/build_folders.conf'), 0o644, folder_config.encode()),
    )


def digest_files(files: tuple[RenderedGuestFile, ...]) -> GuestDigest:
    """Hash the rendered paths, modes, and bytes before adding the digest."""
    entries = sorted((file.path.as_posix(), file.mode, sha256(file.content).hexdigest()) for file in files)
    return GuestDigest(sha256(json.dumps(entries, separators=(',', ':')).encode()).hexdigest())


def guest_digest(configuration: Path | conf.ConfigRead, *, checkout: Path = CHECKOUT) -> GuestDigest:
    return digest_files(guest_files(checkout, configuration, DATA_DEVICE))


if __name__ == '__main__':
    raise SystemExit(main())
