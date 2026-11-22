"""Verify a Hana payload inside the unprivileged command unit."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from io import BufferedIOBase
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from typing import BinaryIO, cast

from scripts.aws_target import hana_protocol


IMAGE_SLOT_POLICY = Path('/var/lib/hana-runtime/config/build_folders.conf')
PAYLOAD_MOUNT = Path('/run/hana-payload')
BUILD_ROOT_KEY = 'roots.build'
PROBE_WAIT_KEY = 'locks.probe_wait_seconds'
PROBE_RETRY_KEY = 'locks.probe_retry_seconds'


@dataclass(frozen=True)
class BundleSlice:
    offset: int
    size: int


@dataclass(frozen=True)
class ImageSlotPolicy:
    build_root: Path
    probe_wait_seconds: float
    probe_retry_seconds: float
    overrides: dict[str, str]

    def copy_path(self, slot: int) -> Path:
        """Locate the numbered copy for the assigned slot."""
        return self.build_root / hana_protocol.REPOSITORY / hana_protocol.COPY_UNIT / str(slot)


def read_slot_policy(path: Path) -> ImageSlotPolicy:
    """Read the capacity, build root, and lock timing from the image."""
    overrides: dict[str, str] = {}
    limits: dict[str, str] = {}
    values: dict[str, str] = {}
    try:
        previous = ''
        for line in path.read_text().splitlines():
            key, separator, value = re.split(r'(?:^|\s)#', line, maxsplit=1)[0].partition('=')
            key = key.strip()
            if separator and (key in (PROBE_WAIT_KEY, PROBE_RETRY_KEY, BUILD_ROOT_KEY)
                    or key.startswith('slots.aws-hana.')) and key not in overrides:
                overrides[key] = line
                values[key] = value.strip()
                if previous.startswith(('# whole:', '# range:')):
                    limits[key] = previous
            previous = line.strip()
        root = Path(values[BUILD_ROOT_KEY])
        wait = float(values[PROBE_WAIT_KEY])
        retry = float(values[PROBE_RETRY_KEY])
        if (not root.is_absolute() or '..' in root.parts or not math.isfinite(wait) or wait < 0
                or not math.isfinite(retry) or retry <= 0
                or any(int(values[key]) <= 0 or not limits[key].startswith('# whole:')
                       for key in ('slots.aws-hana.hana.max', 'slots.aws-hana.hana.max_bytes'))):
            raise ValueError
    except (OSError, KeyError, ValueError):
        raise ValueError('The image slot policy is invalid') from None
    return ImageSlotPolicy(root, wait, retry, {key: limits.get(key, '') + '\n' + line
                                             for key, line in overrides.items()})


def apply_slot_policy(path: Path, policy: ImageSlotPolicy) -> None:
    """Give the runner the image's build root, capacity, and lock timing."""
    try:
        lines: list[str] = []
        for line in path.read_text().splitlines():
            key = line.partition('=')[0].strip()
            if any(key == setting or key.startswith(setting + '.') for setting in policy.overrides):
                if lines and lines[-1].strip().startswith(('# whole:', '# range:')):
                    _ = lines.pop()
            else:
                lines.append(line)
        lines.extend(policy.overrides.values())
        _ = path.write_text('\n'.join(lines) + '\n')
    except OSError:
        raise ValueError('The Hana payload slot policy could not be applied') from None


def download_payload(url: str, archive: BinaryIO, max_bytes: int, timeout_s: int) -> None:
    try:
        _ = subprocess.run(['curl', '--disable', '--fail', '--silent', '--globoff', '--proto', '=https',
                            '--max-time', str(timeout_s), '--max-filesize', str(max_bytes), '--', url],
                           stdout=archive, stderr=subprocess.DEVNULL, check=True, timeout=timeout_s)
        if archive.seek(0, os.SEEK_END) > max_bytes:
            raise ValueError('The Hana payload exceeds its size bound')
    except (OSError, subprocess.SubprocessError):
        raise ValueError('The Hana payload download failed within its size or time bound') from None


def unpack_payload(archive: BufferedIOBase, manifest: hana_protocol.SourceManifest, destination: Path) -> BundleSlice:
    """Verify one archive and return the byte range of its Git bundle."""
    archive_size = archive.seek(0, os.SEEK_END)
    _ = archive.seek(0)
    if hashlib.file_digest(archive, 'sha256').hexdigest() != manifest.bundle_sha256:
        raise ValueError('BundleDigestMismatch: Hana payload sha256 differs')
    _ = archive.seek(0)
    paths: set[str] = set()
    bundles: list[BundleSlice] = []
    try:
        destination.mkdir(mode=0o700)
        with tarfile.TarFile(fileobj=archive, stream=True) as payload:
            for member in payload:
                parts = member.name.split('/')
                if (member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE) or member.sparse is not None
                        or member.size < 0 or member.offset_data + member.size > archive_size
                        or member.name in paths or any(part in ('', '.', '..') for part in parts)):
                    raise ValueError('The Hana payload contains an invalid archive member')
                paths.add(member.name)
                if member.name == 'source.bundle' and member.size > 0:
                    bundles.append(BundleSlice(member.offset_data, member.size))
                    continue
                if len(parts) < 2 or parts[0] != 'runtime':
                    raise ValueError('The Hana payload contains a file outside the runtime')
                target = destination.joinpath(*parts[1:])
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                source = payload.extractfile(member)
                if source is None:
                    raise ValueError('The Hana payload member has no data')
                with source, target.open('xb') as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o700 if member.mode & 0o111 else 0o600)
    except (OSError, tarfile.TarError):
        raise ValueError('The Hana payload archive is invalid') from None
    if not bundles or not {'runtime/abi', 'runtime/runner.py'} <= paths:
        raise ValueError('The Hana payload is missing the bundle, ABI, or runner')
    expected_abi = f'{manifest.runtime_abi}\n'.encode()
    with (destination / 'abi').open('rb') as marker:
        payload_abi = marker.read(len(expected_abi) + 1)
    if manifest.runtime_abi != hana_protocol.RUNTIME_ABI or payload_abi != expected_abi:
        raise ValueError('The Hana payload ABI differs from its manifest or image')
    return bundles[0]


def main(max_bytes: int, download_timeout_s: int, command_timeout_s: float) -> int:
    """Start the transferred runner with the verified local source."""
    action = 'execute'
    try:
        content = sys.stdin.buffer.read(hana_protocol.CONTROL_MAX_BYTES + 1)
        if len(content) > hana_protocol.CONTROL_MAX_BYTES:
            raise ValueError('The Hana payload input exceeds its protocol bound')
        row = cast(object, json.loads(content))
        if not isinstance(row, dict):
            raise ValueError('The Hana payload input must be an object')
        fields = cast(dict[str, object], row)
        selected = fields.get('action', 'execute')
        if selected not in ('execute', 'source-update'):
            raise ValueError('The Hana payload action is invalid')
        action = selected
        request = hana_protocol.decode_request(json.dumps(fields['request']).encode())
        policy = read_slot_policy(IMAGE_SLOT_POLICY)
        checkout = hana_protocol.main_checkout(Path.home())
        checkout.mkdir(parents=True, exist_ok=True)
        with (tempfile.TemporaryFile(dir=checkout) as archive,
              tempfile.TemporaryDirectory(dir=PAYLOAD_MOUNT) as workspace,
              os.fdopen(os.dup(sys.stdout.fileno()), 'wb') as report_start):
            download_payload(request.manifest.bundle_url, archive, max_bytes, download_timeout_s)
            runtime = Path(workspace) / 'runtime'
            bundle = unpack_payload(archive, request.manifest, runtime)
            configuration = runtime / 'config'
            apply_slot_policy(configuration / 'build_folders.conf', policy)
            start_fd = report_start.fileno()
            result = subprocess.run([sys.executable, '-I', str(runtime / 'runner.py'),
                '--bundle-fd', str(archive.fileno()), '--bundle-offset', str(bundle.offset),
                '--bundle-size', str(bundle.size), '--command-timeout-s', str(command_timeout_s),
                '--build-start-fd', str(start_fd)],
                input=content, env={**os.environ, 'CONF_DIR': str(configuration)},
                pass_fds=(archive.fileno(), start_fd), check=False)
            if result.returncode == 0:
                return 0
            raise ValueError('The Hana runner failed to start or exited without a result')
    except ValueError as error:
        reason = ' '.join(str(error).splitlines())
    except (OSError, KeyError, subprocess.SubprocessError):
        reason = 'The Hana payload could not start the runner'
    outcome = (hana_protocol.encode_source_outcome(hana_protocol.HanaSourceUpdateRefused(reason))
               if action == 'source-update' else hana_protocol.encode_outcome(hana_protocol.HanaExecutionRefused(reason)))
    _ = sys.stdout.buffer.write(outcome)
    return 0
