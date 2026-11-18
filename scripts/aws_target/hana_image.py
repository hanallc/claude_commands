"""Read the built Hana NixOS image and save its registration state."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
from typing import cast

from scripts.aws_target import hana_render, ledger, remote
from scripts.aws_target.aws_cli import AwsExecutor
from scripts.lib import conf
from scripts.lib.atomic_file import write_text_atomic


RECORD = Path('hana/image.json')
# The qemu-img input format for each image file suffix that the NixOS Amazon builder writes.
SOURCE_FORMATS = {'.vhd': 'vpc', '.raw': 'raw', '.img': 'raw', '.qcow2': 'qcow2'}
BOOT_MODES = frozenset(('legacy-bios', 'uefi'))
# Coldsnap parses --max-attempts as a u8 argument.
COLDSNAP_MAX_WAIT_ATTEMPTS = 255


class ImageRefused(ValueError):
    """The built image or its saved registration cannot be used."""


@dataclass(frozen=True)
class BuiltImage:
    """One NixOS Amazon image result and the name its registration uses."""
    name: str
    store: Path
    file: Path
    source_format: str
    boot_mode: str
    logical_bytes: int


@dataclass(frozen=True)
class PendingImageRegistration:
    """The uploaded Hana snapshot still needs an image registration."""
    name: str
    snapshot: str


@dataclass(frozen=True)
class RegisteredImage:
    """The registered Hana image and the network that the Hana stack uses."""
    name: str
    image: str
    owner: str
    vpc: str
    subnet: str
    digest: hana_render.GuestDigest | hana_render.GuestDigestUnknown = hana_render.GuestDigestUnknown()


@dataclass(frozen=True)
class NoRegisteredImage:
    """No Hana image registration is saved."""


def read_built(result: Path) -> BuiltImage:
    """Read the image file, format, and boot mode from the builder's image-info.json."""
    store = result.resolve()
    try:
        info = ledger.mapping(cast(object, json.loads((store / 'nix-support/image-info.json').read_text())))
        file = Path(ledger.text(info, 'file')).resolve()
        label = ledger.text(info, 'label')
        boot_mode = ledger.text(info, 'boot_mode')
        logical_bytes = int(ledger.text(info, 'logical_bytes'))
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ImageRefused(f'{result}: no readable NixOS Amazon image information') from error
    source_format = SOURCE_FORMATS.get(file.suffix)
    if source_format is None or not file.is_relative_to(store) or not file.is_file():
        raise ImageRefused(f'{result}: the image file is missing or has an unknown format')
    if boot_mode not in BOOT_MODES:
        raise ImageRefused(f'{result}: unknown boot mode {boot_mode}')
    # The store hash makes the name change with every different image build.
    return BuiltImage(f'nixos-hana-{label}-{store.name.split("-", 1)[0][:12]}', store, file, source_format,
                      boot_mode, logical_bytes)


def artifact_digest(store: Path, execute: AwsExecutor, timeout: float) -> hana_render.GuestDigest:
    """Read the digest from the renderer output in this image's build closure."""
    def query(option: str, path: Path) -> list[str]:
        done = execute(['nix-store', '--query', option, str(path)], capture_output=True,
            text=True, timeout=timeout, check=True)
        return done.stdout.splitlines()

    try:
        derivers = query('--deriver', store)
        if len(derivers) != 1 or not derivers[0].endswith('.drv'):
            raise ValueError('The image derivation is unknown.')
        dependencies = query('--requisites', Path(derivers[0]))
        renderers = [Path(entry) for entry in dependencies if entry.endswith('-hana-guest-files.drv')]
        if len(renderers) != 1:
            raise ValueError('The image must have one Hana renderer.')
        outputs = query('--outputs', renderers[0])
        if len(outputs) != 1:
            raise ValueError('The Hana renderer must have one output.')
        document = ledger.mapping(cast(object, json.loads((Path(outputs[0]) / 'safety.json').read_text())))
        digest = hana_render.decode_guest_digest(document.get('guest_digest'))
        if isinstance(digest, hana_render.GuestDigest):
            return digest
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    raise ImageRefused('The Hana image has no verified guest digest. Build the Hana image from the current input.')


def setup_image_is_current(setup: dict[str, object], configuration: Path | conf.ConfigRead) -> bool:
    digest = hana_render.decode_guest_digest(setup.get('guest_digest'))
    if not isinstance(digest, hana_render.GuestDigest):
        return False
    try:
        return digest == hana_render.guest_digest(configuration)
    except (OSError, ValueError):
        return False


def deployed_image_is_current(directory: Path, deployment: str, instance: str,
                              configuration: Path | conf.ConfigRead) -> bool:
    """Check the image setup confirmed for the lifecycle's exact box."""
    try:
        setup = remote.read_record(directory / 'hana/setup.json')
    except (OSError, ValueError):
        return False
    return (setup.get('deployment') == deployment and setup.get('instance') == instance
            and setup_image_is_current(setup, configuration))


def saved(directory: Path) -> PendingImageRegistration | RegisteredImage | NoRegisteredImage:
    path = directory / RECORD
    if not path.exists():
        return NoRegisteredImage()
    record = remote.read_record(path)
    try:
        state = record.get('state', 'registered')
        if state == 'pending':
            return PendingImageRegistration(ledger.text(record, 'name'), ledger.text(record, 'snapshot'))
        if state != 'registered':
            raise ValueError('unknown registration state')
        return RegisteredImage(*(ledger.text(record, key) for key in ('name', 'image', 'owner', 'vpc', 'subnet')),
            digest=hana_render.decode_guest_digest(record.get('guest_digest')))
    except (KeyError, TypeError, ValueError) as error:
        raise ImageRefused(f'{path}: the saved Hana image record is incomplete') from error


def save(directory: Path, registration: PendingImageRegistration | RegisteredImage) -> Path:
    path = directory / RECORD
    path.parent.mkdir(parents=True, exist_ok=True)
    record: dict[str, str] = {'name': registration.name}
    if isinstance(registration, PendingImageRegistration):
        record.update(state='pending', snapshot=registration.snapshot)
    else:
        record.update(state='registered', image=registration.image, owner=registration.owner,
                      vpc=registration.vpc, subnet=registration.subnet)
        if isinstance(registration.digest, hana_render.GuestDigest):
            record['guest_digest'] = registration.digest.value
    write_text_atomic(path, json.dumps(record, sort_keys=True) + '\n')
    return path
