"""Read the built Hana NixOS image and keep its registered identity in local deployment state."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import cast

from scripts.aws_target import ledger, remote
from scripts.lib.atomic_file import write_text_atomic


RECORD = Path('hana/image.json')
# The qemu-img input format for each image file suffix that the NixOS Amazon builder writes.
SOURCE_FORMATS = {'.vhd': 'vpc', '.raw': 'raw', '.img': 'raw', '.qcow2': 'qcow2'}
BOOT_MODES = frozenset(('legacy-bios', 'uefi'))


class ImageRefused(ValueError):
    """The built image or its saved registration cannot be used."""


@dataclass(frozen=True)
class BuiltImage:
    """One NixOS Amazon image result and the name its registration uses."""
    name: str
    file: Path
    source_format: str
    boot_mode: str
    logical_bytes: int


@dataclass(frozen=True)
class RegisteredImage:
    """The registered Hana image and the network that the Hana stack uses."""
    name: str
    image: str
    owner: str
    vpc: str
    subnet: str


@dataclass(frozen=True)
class NoRegisteredImage:
    """No Hana image registration is saved."""


def read_built(result: Path) -> BuiltImage:
    """Read the image file, format, and boot mode from the builder's image-info.json."""
    store = result.resolve()
    try:
        info = ledger.mapping(cast(object, json.loads((store / 'nix-support/image-info.json').read_text())))
        file = Path(ledger.text(info, 'file'))
        label = ledger.text(info, 'label')
        boot_mode = ledger.text(info, 'boot_mode')
        logical_bytes = int(ledger.text(info, 'logical_bytes'))
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ImageRefused(f'{result}: no readable NixOS Amazon image information') from error
    source_format = SOURCE_FORMATS.get(file.suffix)
    if source_format is None or not file.is_file():
        raise ImageRefused(f'{result}: the image file is missing or has an unknown format')
    if boot_mode not in BOOT_MODES:
        raise ImageRefused(f'{result}: unknown boot mode {boot_mode}')
    # The store hash makes the name change with every different image build.
    return BuiltImage(f'nixos-hana-{label}-{store.name.split("-", 1)[0][:12]}', file, source_format,
                      boot_mode, logical_bytes)


def saved(directory: Path) -> RegisteredImage | NoRegisteredImage:
    path = directory / RECORD
    if not path.exists():
        return NoRegisteredImage()
    record = remote.read_record(path)
    try:
        return RegisteredImage(*(ledger.text(record, key) for key in ('name', 'image', 'owner', 'vpc', 'subnet')))
    except (KeyError, TypeError, ValueError) as error:
        raise ImageRefused(f'{path}: the saved Hana image record is incomplete') from error


def save(directory: Path, registered: RegisteredImage) -> Path:
    path = directory / RECORD
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, json.dumps({'name': registered.name, 'image': registered.image, 'owner': registered.owner,
                                        'vpc': registered.vpc, 'subnet': registered.subnet}, sort_keys=True) + '\n')
    return path
