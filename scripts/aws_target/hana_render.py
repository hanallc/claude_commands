"""Render the Hana guest files from the repository configuration."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import cast

from scripts.aws_target import hana_guest, hana_receiver, hana_runner
from scripts.lib import conf


CHECKOUT = Path(__file__).resolve().parents[2]
DATA_DEVICE = Path('/dev/sdf')
HANA_TARGET_ALIAS_KEY = 'host'
HANA_GUEST_KEYS = (
    'enabled', 'guard_grace_seconds', 'watchdog_check_seconds', 'permit_max_seconds',
    'step_cpu_limit', 'step_memory_gib', 'mount_timeout_seconds', 'aws_timeout_s', 'control_poll_seconds',
    *hana_runner.EXECUTION_CONFIG_KEYS, 'source_max_bytes', 'source_download_timeout_s',
    'source_git_timeout_s', 'source_allowed_environment',
)


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
    """Check whether a Hana config key can change a rendered guest value."""
    if key == HANA_TARGET_ALIAS_KEY:
        return True
    settings = conf.settings(configuration.read_text(encoding='utf-8'))
    host_name = guest_host_name(settings)
    return any(conf.effective_key(settings, base, host_name=host_name) == key for base in HANA_GUEST_KEYS)


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


def guest_files(checkout: Path, configuration: Path, device: Path) -> tuple[RenderedGuestFile, ...]:
    """Render the guest units and their runtime files from one config view."""
    hana_settings = conf.settings(configuration.read_text(encoding='utf-8'))
    host_name = guest_host_name(hana_settings)
    guest_config = resolved_config_text(hana_settings, HANA_GUEST_KEYS, host_name)
    parsed = conf.ConfigRead(configuration, configuration, conf.settings(guest_config), False)
    control = (*hana_guest.render_files(hana_guest.read_settings(parsed), device=device),
               *hana_receiver.render_files(hana_receiver.read_settings(parsed)))
    files = [RenderedGuestFile(Path(file.path.name), file.mode, file.content.encode()) for file in control]
    for entry in (checkout / 'scripts/aws_target/hana_render.files').read_text().splitlines():
        relative = Path(entry)
        if not entry or relative.is_absolute() or '..' in relative.parts:
            raise ValueError('The runtime file list contains an unsafe path')
        if entry == 'config/hana_aws.conf':
            content = guest_config.encode()
        elif entry == 'config/build_targets.conf':
            settings = conf.settings((checkout / relative).read_text(encoding='utf-8'))
            content = resolved_config_text(settings, hana_runner.EXECUTION_TIMEOUT_KEYS, host_name).encode()
        elif entry == 'config/copies.conf':
            settings = conf.settings((checkout / relative).read_text(encoding='utf-8'))
            content = config_text({key: setting for key, setting in settings.items()
                if key != 'root' and key.rpartition('.')[0] != 'root'}).encode()
        else:
            content = (checkout / relative).read_bytes()
        files.append(RenderedGuestFile(Path('runtime') / relative, 0o644, content))
    files.append(RenderedGuestFile(Path('runtime/runner.py'), 0o644,
        b'import sys\nfrom pathlib import Path\n'
        + b'sys.path.insert(0, str(Path(__file__).resolve().parent))\n'
        + b'from scripts.aws_target.hana_runner import main\nraise SystemExit(main())\n'))
    return tuple(files)


def digest_files(files: tuple[RenderedGuestFile, ...]) -> GuestDigest:
    """Hash the rendered paths, modes, and bytes before adding the digest."""
    entries = sorted((file.path.as_posix(), file.mode, sha256(file.content).hexdigest()) for file in files)
    return GuestDigest(sha256(json.dumps(entries, separators=(',', ':')).encode()).hexdigest())


def guest_digest(configuration: Path, *, checkout: Path = CHECKOUT) -> GuestDigest:
    return digest_files(guest_files(checkout, configuration, DATA_DEVICE))


if __name__ == '__main__':
    raise SystemExit(main())
