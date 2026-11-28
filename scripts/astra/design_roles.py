#!/usr/bin/env python3
"""Keep one designer and reviewer per design, with explicit placement and phase access.

Only the pinned authority changes placements. Local lifecycle actions keep thread identity,
assignment evidence and pending messages when a server or acknowledgement is lost.
"""
from __future__ import annotations

try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from scripts.direct import pass_record as pass_recorder

from scripts.lib import unit_runs

import argparse
import configparser
import fcntl
import hashlib
import importlib
import json
import os
import re
import shlex
import socket
import subprocess
import sys
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, cast
from uuid import UUID, uuid4

from scripts.astra import astra_work
from scripts.agents import worker_lifecycle
from scripts.direct import recorder_state
from scripts.lib import conf_reads
from scripts.lib.atomic_file import write_text_atomic
from scripts.production import staff

ROOT = Path(__file__).resolve().parents[2]
Role = Literal['designer', 'reviewer']
DESIGNER_SANDBOX = 'danger-full-access'


class AstraCensusProjection(Protocol):
    def HeldDesignRole(self, design_id: str, role_id: str) -> object: ...
    def hold(self, role: Literal['designer', 'reviewer'], lifetime: object, staff_id: str) -> object: ...
    def release(self, staff_id: str) -> None: ...
    def live_roles(self, membership: staff.StaffSnapshot, *,
                   selection: frozenset[staff.StaffId]) -> Sequence[Mapping[str, object]]: ...


def _census_projection() -> AstraCensusProjection:
    return cast(AstraCensusProjection, cast(object, importlib.import_module('scripts.astra.astra_roles')))


@dataclass(frozen=True)
class DesignUnbound:
    pass


@dataclass(frozen=True)
class IndependentCheckout:
    """The checkout keeps its own path."""


@dataclass(frozen=True)
class UnitCheckout:
    """The unit's staff row supplies the checkout path."""

    owner: staff.StaffId


@dataclass(frozen=True)
class ConfiguredCheckout:
    """An independent repository keeps its configured path."""

    path: str


Checkout = IndependentCheckout | UnitCheckout
INDEPENDENT_CHECKOUT = IndependentCheckout()


@dataclass(frozen=True)
class DesignBoundToPhase:
    run_directory: str
    phase: str
    repository: str
    output_paths: tuple[str, ...]
    checkout: Checkout = field(default=INDEPENDENT_CHECKOUT, kw_only=True)


Binding = DesignUnbound | DesignBoundToPhase
UNBOUND = DesignUnbound()


@dataclass(frozen=True)
class KeepBinding:
    """A rebind keeps the saved binding."""


KEEP_BINDING = KeepBinding()


@dataclass(frozen=True)
class PhaseBoundDesigner:
    binding_generation: int
    launch_sandbox: str


@dataclass(frozen=True)
class ReadOnlyReviewer:
    pass


ThreadAccessPolicy = PhaseBoundDesigner | ReadOnlyReviewer


@dataclass(frozen=True)
class LegacyCreationState:
    """An old claim cannot prove that thread creation did not start."""


LEGACY_CREATION_STATE = LegacyCreationState()


@dataclass(frozen=True, init=False)
class RoleNotOpened:
    claim: str
    creation: Literal['not_started', 'outcome_unknown']

    def __init__(self, claim: str = '',
                 creation: Literal['not_started', 'outcome_unknown'] | LegacyCreationState = LEGACY_CREATION_STATE) -> None:
        object.__setattr__(self, 'claim', claim)
        object.__setattr__(self, 'creation', ('outcome_unknown' if claim else 'not_started')
                           if isinstance(creation, LegacyCreationState) else creation)


@dataclass(frozen=True)
class HeldWorker:
    staff_id: staff.StaffId

    @property
    def thread_id(self) -> str:
        member = staff.by_id(self.staff_id)
        return member.current_session if member is not None else ''


@dataclass(frozen=True)
class RoleOpen(HeldWorker):
    pass


@dataclass(frozen=True)
class RoleWorkerPublished(HeldWorker):
    """Staff recorded the worker. Startup has not confirmed the pane."""


@dataclass(frozen=True)
class RolePausedForMove(HeldWorker):
    pass


@dataclass(frozen=True)
class RoleNeedsRecovery(HeldWorker):
    reason: str


@dataclass(frozen=True)
class RoleEnded(HeldWorker):
    pass


Lifecycle = RoleNotOpened | RoleWorkerPublished | RoleOpen | RolePausedForMove | RoleNeedsRecovery | RoleEnded


@dataclass(frozen=True)
class DesignPlacement:
    design_id: str
    host: str
    generation: int


@dataclass(frozen=True)
class RoleParentUnrecorded:
    """The role is unclaimed or predates the saved parent."""


@dataclass(frozen=True)
class RoleIdentityUnresolved:
    """The legacy record lacks proof of the worker or its parent."""

    reason: str
    saved_parent: staff.StaffId | RoleParentUnrecorded = field(default_factory=RoleParentUnrecorded)


@dataclass(frozen=True)
class RoleInstance:
    role: Role
    name: str
    lifecycle: Lifecycle
    assignment_claim: str = ""
    parent: staff.StaffId | RoleParentUnrecorded | RoleIdentityUnresolved = field(default_factory=RoleParentUnrecorded)


@dataclass(frozen=True)
class HeldDesign:
    placement: DesignPlacement
    repository_id: str
    repository: str
    document: str
    area: str
    created_at_utc: str
    binding: Binding
    roles: tuple[RoleInstance, ...]
    checkout: Checkout = field(default=INDEPENDENT_CHECKOUT, kw_only=True)


def instance_name(instance: RoleInstance, membership: staff.StaffSnapshot | None = None) -> str:
    if isinstance(instance.lifecycle, HeldWorker) and instance.lifecycle.staff_id:
        membership = staff.snapshot() if membership is None else membership
        member = membership.by_id(instance.lifecycle.staff_id)
        return member.name if member is not None else ''
    return instance.name


def _checkout_path(repository: str, checkout: Checkout, membership: staff.StaffSnapshot | None = None) -> str:
    if isinstance(checkout, IndependentCheckout):
        return repository
    membership = staff.snapshot() if membership is None else membership
    owner = membership.by_id(checkout.owner)
    if owner is None or owner.kind != 'unit':
        raise RoleRefused('checkout owner is absent: ' + checkout.owner)
    worktree = owner.worktree
    if not isinstance(worktree, (staff.PresentWorktree, staff.RemovedWorktree)):
        raise RoleRefused('checkout owner has no worktree: ' + checkout.owner)
    return str(worktree.path)


def design_repository(design: HeldDesign, membership: staff.StaffSnapshot | None = None) -> str:
    return _checkout_path(design.repository, design.checkout, membership)


def binding_repository(binding: DesignBoundToPhase, membership: staff.StaffSnapshot | None = None) -> str:
    return _checkout_path(binding.repository, binding.checkout, membership)


def _registered_path(value: object, host: str, membership: staff.StaffSnapshot) -> str:
    if isinstance(value, str):
        if not Path(value).is_absolute():
            raise RoleRefused('checkout mapping must name an absolute path')
        return value
    fields = _object(value)
    if set(fields) == {'configured_checkout'}:
        return _registered_path(_text(fields, 'configured_checkout'), host, membership)
    if set(fields) != {'repository_owner'}:
        raise RoleRefused('checkout mapping needs one staff owner')
    reference = staff.StaffId(_text(fields, 'repository_owner'))
    owner = membership.by_id(reference)
    if (owner is None or not isinstance(owner.worktree, (staff.PresentWorktree, staff.RemovedWorktree))
            or owner.worktree.machine != host):
        raise RoleRefused('checkout owner is unavailable on ' + host)
    return _checkout_path('', UnitCheckout(reference), membership)


def _placement_fields(value: object) -> dict[str, object]:
    if isinstance(value, str):
        return {'checkout': value}
    fields = _object(value)
    return {'checkout': _text(fields, 'configured_checkout')} if 'configured_checkout' in fields else fields


def _placement_checkout(placement: dict[str, object]) -> str:
    if 'repository_owner' in placement:
        return _checkout_path('', UnitCheckout(staff.StaffId(_text(placement, 'repository_owner'))))
    return _text(placement, 'checkout')


def _binding_checkout(binding: Binding, repository: str, checkout: Checkout) -> Binding:
    if isinstance(binding, DesignBoundToPhase) and Path(binding.repository).resolve() == Path(repository).resolve():
        return replace(binding, checkout=checkout)
    return binding


@dataclass(frozen=True)
class PendingMessage:
    id: str
    source: str
    text: str


class RoleRefused(ValueError):
    pass


class DamagedDesignRecord(RoleRefused):
    path: Path
    field: str

    def __init__(self, path: Path, field: str) -> None:
        self.path = path
        self.field = field
        super().__init__('missing or invalid ' + field.rsplit('.', 1)[-1])


def state_root() -> Path:
    return Path(os.environ.get('DESIGN_ROLES_STATE') or '~/.local/state/astra/design-roles').expanduser()


def ownership_root() -> Path:
    return Path(os.environ.get('DESIGN_OWNERSHIP_STATE') or '~/.local/state/astra/design-ownership').expanduser()


def local_host() -> str:
    return os.environ.get('DESIGN_ROLES_HOST') or socket.gethostname().split('.', 1)[0]


def design_directory(design_id: str) -> Path:
    if str(UUID(design_id)) != design_id:
        raise RoleRefused('design id must be a UUID')
    return state_root() / design_id


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RoleRefused('expected a JSON object')
    return cast(dict[str, object], value)


def _read(path: Path) -> dict[str, object]:
    return worker_lifecycle.read_worker_state(path)


def _text(fields: dict[str, object], key: str) -> str:
    value = fields.get(key)
    if not isinstance(value, str) or not value:
        raise RoleRefused('missing or invalid ' + key)
    return value


def _integer(fields: dict[str, object], key: str) -> int:
    value = fields.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise RoleRefused('missing or invalid ' + key)
    return value


def _atomic(path: Path, value: object) -> None:
    _ = worker_lifecycle.read_worker_value(path)
    _ = worker_lifecycle.validate_worker_state_value(path, value)
    write_text_atomic(path, json.dumps(value, indent=2, sort_keys=True) + '\n')


@contextmanager
def _lock(directory: Path) -> Generator[None]:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'lock').open('a', encoding='utf-8') as handle:
        worker_lifecycle.acquire_flock(handle, mode=fcntl.LOCK_EX)
        try:
            yield
        finally:
            worker_lifecycle.acquire_flock(handle, mode=fcntl.LOCK_UN)


def _tag(value: Binding | Lifecycle | ThreadAccessPolicy) -> dict[str, object]:
    fields = {'kind': type(value).__name__, **cast(dict[str, object], asdict(value))}
    if isinstance(value, DesignBoundToPhase):
        del fields['checkout']
        del fields['repository']
        fields.update(_checkout_fields(value.repository, value.checkout))
    return fields


def _checkout_fields(repository: str, checkout: Checkout) -> dict[str, object]:
    return {'repository_owner': checkout.owner} if isinstance(checkout, UnitCheckout) else {'repository': repository}


def _saved_checkout(fields: dict[str, object]) -> tuple[str, Checkout]:
    if 'repository_owner' in fields:
        return '', UnitCheckout(staff.StaffId(_text(fields, 'repository_owner')))
    return _text(fields, 'repository'), INDEPENDENT_CHECKOUT


def _read_checkout(fields: dict[str, object]) -> tuple[str, Checkout]:
    repository, checkout = _saved_checkout(fields)
    return _checkout_path(repository, checkout), checkout


def _binding(fields: dict[str, object]) -> Binding:
    if fields.get('kind') == 'DesignUnbound':
        return DesignUnbound()
    if fields.get('kind') != 'DesignBoundToPhase':
        raise RoleRefused('unknown binding')
    paths = fields.get('output_paths')
    if not isinstance(paths, list) or not paths or any(not isinstance(path, str) for path in cast(list[object], paths)):
        raise RoleRefused('binding needs exact output paths')
    repository, checkout = _saved_checkout(fields)
    return DesignBoundToPhase(_text(fields, 'run_directory'), _text(fields, 'phase'),
                              repository, tuple(cast(list[str], paths)), checkout=checkout)


def _lifecycle(fields: dict[str, object]) -> Lifecycle:
    kind = fields.get('kind')
    if kind == 'RoleNotOpened':
        claim = fields.get('claim', '')
        if not isinstance(claim, str):
            raise RoleRefused('invalid launch claim')
        creation = fields.get('creation', 'outcome_unknown' if claim else 'not_started')
        if creation not in ('not_started', 'outcome_unknown'):
            raise RoleRefused('invalid thread creation state')
        return RoleNotOpened(claim, creation)
    if kind == 'RoleEnded' and fields.get('staff_id') == '':
        return RoleEnded(staff.StaffId(''))
    reference = staff.StaffId(_text(fields, 'staff_id'))
    if kind == 'RoleOpen':
        return RoleOpen(reference)
    if kind == 'RoleWorkerPublished':
        return RoleWorkerPublished(reference)
    if kind == 'RolePausedForMove':
        return RolePausedForMove(reference)
    if kind == 'RoleNeedsRecovery':
        return RoleNeedsRecovery(reference, _text(fields, 'reason'))
    if kind == 'RoleEnded':
        return RoleEnded(reference)
    raise RoleRefused('unknown lifecycle')


def read_design(design_id: str) -> HeldDesign:
    return _load_design(design_id, repair_empty_repository=False)


def _load_design(design_id: str, *, repair_empty_repository: bool, local_staff: bool = False) -> HeldDesign:
    path = design_directory(design_id) / 'design.json'
    try:
        design = _decode_design(design_id, _read(path), repair_empty_repository=repair_empty_repository)
        return _published_workers(design, local_staff=local_staff)
    except RoleRefused as error:
        raise worker_lifecycle.WorkerStateUnreadable(path, str(error)) from error


def _decode_design(design_id: str, fields: dict[str, object], *,
                        repair_empty_repository: bool = False) -> HeldDesign:
    return _parse_design(design_id, fields, repair_empty_repository=repair_empty_repository)()


def _parse_design(design_id: str, fields: dict[str, object], *,
                  repair_empty_repository: bool = False) -> Callable[[], HeldDesign]:
    """Validate the saved fields and defer staff lookups."""
    path = design_directory(design_id) / 'design.json'
    if fields.get('schema') != 1:
        raise RoleRefused('unknown design schema')
    placement = _object(fields.get('placement'))
    if placement.get('design_id') != design_id:
        raise RoleRefused('design identity differs from directory')
    entries = fields.get('roles')
    if not isinstance(entries, list):
        raise RoleRefused('missing roles')
    instances: list[RoleInstance] = []
    for value in cast(list[object], entries):
        entry = _object(value)
        role = entry.get('role')
        if role not in ('designer', 'reviewer') or any(instance.role == role for instance in instances):
            raise RoleRefused('invalid or duplicate role')
        claim = entry.get('assignment_claim', '')
        if not isinstance(claim, str):
            raise RoleRefused('invalid assignment claim')
        lifecycle = _lifecycle(_object(entry.get('lifecycle')))
        name = '' if isinstance(lifecycle, HeldWorker) and lifecycle.staff_id else _text(entry, 'name')
        saved_parent = staff.StaffId(_text(entry, 'parent')) if 'parent' in entry else RoleParentUnrecorded()
        parent = (RoleIdentityUnresolved(_text(entry, 'identity_problem'), saved_parent)
                  if 'identity_problem' in entry else saved_parent)
        instances.append(RoleInstance(role, name, lifecycle, claim, parent))
    if {instance.role for instance in instances} != {'designer', 'reviewer'}:
        raise RoleRefused('design must record both role slots')
    repository, checkout = _saved_checkout(fields)
    document = _text(fields, 'document')
    if isinstance(checkout, IndependentCheckout):
        for key, value in (('repository', repository), ('document', document)):
            if not Path(value).is_absolute():
                raise RoleRefused(key + ' must be absolute')
    binding_fields = _object(fields.get('binding'))
    empty_repository = (binding_fields.get('kind') == 'DesignBoundToPhase'
                        and binding_fields.get('repository') == '')
    if empty_repository:
        binding_fields = {**binding_fields, **_checkout_fields(repository, checkout)}
    design_placement = DesignPlacement(design_id, _text(placement, 'host'), _integer(placement, 'generation'))
    repository_id = _text(fields, 'repository_id')
    area = _text(fields, 'area')
    created = _text(fields, 'created_at_utc')
    binding = _binding(binding_fields)
    if empty_repository and not repair_empty_repository:
        raise DamagedDesignRecord(path, 'binding.repository')

    def resolve() -> HeldDesign:
        root = _checkout_path(repository, checkout)
        if not Path(root).is_absolute():
            raise RoleRefused('repository must be absolute')
        resolved_document = str((Path(root) / document).resolve()) if isinstance(checkout, UnitCheckout) else document
        resolved_binding = (replace(binding, repository=binding_repository(binding))
                            if isinstance(binding, DesignBoundToPhase) else binding)
        roles = tuple(replace(instance, name=instance_name(instance)) for instance in instances)
        return HeldDesign(design_placement, repository_id, root, resolved_document, area, created,
                          resolved_binding, roles, checkout=checkout)

    return resolve


def _design_fields(design: HeldDesign) -> dict[str, object]:
    if isinstance(design.binding, DesignBoundToPhase) and not design.binding.repository:
        raise RoleRefused('missing or invalid binding.repository')
    fields = asdict(design)
    del fields['checkout']
    del fields['repository']
    fields.update(_checkout_fields(design.repository, design.checkout))
    if isinstance(design.checkout, UnitCheckout):
        fields['document'] = Path(design.document).relative_to(design.repository).as_posix()
    fields['schema'] = 1
    binding_fields = _tag(design.binding)
    if isinstance(design.binding, DesignBoundToPhase):
        binding_fields['output_paths'] = list(design.binding.output_paths)
    fields['binding'] = binding_fields
    roles: list[dict[str, object]] = []
    for role in design.roles:
        lifecycle = _tag(role.lifecycle)
        entry: dict[str, object] = {'role': role.role, 'lifecycle': lifecycle, 'assignment_claim': role.assignment_claim}
        if isinstance(role.parent, str):
            entry['parent'] = role.parent
        elif isinstance(role.parent, RoleIdentityUnresolved):
            entry['identity_problem'] = role.parent.reason
            if isinstance(role.parent.saved_parent, str):
                entry['parent'] = role.parent.saved_parent
        if not isinstance(role.lifecycle, HeldWorker) or not role.lifecycle.staff_id:
            entry['name'] = role.name
        roles.append(entry)
    fields['roles'] = roles
    return fields


def _save(design: HeldDesign) -> None:
    fields = _design_fields(design)
    _ = _decode_design(design.placement.design_id, fields)
    _atomic(design_directory(design.placement.design_id) / 'design.json', fields)


def _instance(design: HeldDesign, role: Role) -> RoleInstance:
    return next(instance for instance in design.roles if instance.role == role)


def _set_instance(design: HeldDesign, instance: RoleInstance) -> HeldDesign:
    return replace(design, roles=tuple(instance if value.role == instance.role else value for value in design.roles))


def _published_workers(design: HeldDesign, *, local_staff: bool = False) -> HeldDesign:
    """Read the worker ID from the staff commit that published the role."""
    pending = [instance for instance in design.roles if isinstance(instance.parent, str)
               and isinstance(instance.lifecycle, RoleNotOpened) and instance.lifecycle.creation == 'outcome_unknown']
    if not pending:
        return design
    record = staff.local_snapshot() if local_staff else staff.snapshot()
    for instance in pending:
        claim = staff.DesignWorkerClaim(design.placement.design_id, instance.role)
        if claim in record.design_workers:
            reference = record.design_workers[claim]
            published = replace(instance, lifecycle=RoleWorkerPublished(reference))
            design = _set_instance(design, replace(published, name=instance_name(published, record)))
    return design


def _set_lifecycle(design: HeldDesign, role: Role, lifecycle: Lifecycle) -> HeldDesign:
    instances: list[RoleInstance] = []
    for instance in design.roles:
        if instance.role == role:
            instance = replace(instance, lifecycle=lifecycle)
            instance = replace(instance, name=instance_name(instance))
        instances.append(instance)
    return replace(design, roles=tuple(instances))


def authority_request(operation: str, payload: dict[str, object]) -> dict[str, object]:
    """Send an ownership request after releasing all design locks."""
    config = configparser.ConfigParser()
    path = Path(os.environ.get('DESIGN_ROLES_CONFIG') or ROOT / 'config/design_roles.conf')
    try:
        config.read_string(conf_reads.read_text(path))
    except FileNotFoundError as error:
        raise RoleRefused('ownership authority is not configured') from error
    authority = config.get('ownership', 'host')
    if authority == local_host():
        return _authority_local(operation, payload)
    command = ['ssh', '-o', 'BatchMode=yes', authority,
               '~/.claude/scripts/lib/py ~/.claude/scripts/astra/design_roles.py authority ' + shlex.quote(operation)]
    result = subprocess.run(command, input=json.dumps(payload), capture_output=True, text=True, timeout=30, check=False)
    if result.returncode:
        raise RoleRefused('ownership authority unavailable: ' + (result.stderr or result.stdout).strip())
    return _object(cast(object, json.loads(result.stdout)))


def register_repository(repository_id: str, checkouts: Mapping[str, str | ConfiguredCheckout | UnitCheckout]) -> None:
    """Register explicit unit owners or independent checkout paths."""
    if not repository_id or not checkouts:
        raise RoleRefused('repository id and checkout mappings are required')
    _ = authority_request('register', {'repository_id': repository_id, 'checkouts': {
        host: ({'repository_owner': checkout.owner} if isinstance(checkout, UnitCheckout) else
               {'configured_checkout': checkout.path} if isinstance(checkout, ConfiguredCheckout) else checkout)
        for host, checkout in checkouts.items()}})


def _authority_local(operation: str, payload: dict[str, object]) -> dict[str, object]:
    root = ownership_root()
    with _lock(root):
        membership = staff.snapshot()
        path = root / 'repositories.json'
        repositories = _read(path) if path.exists() else {}
        repository_id = _text(payload, 'repository_id')
        if operation == 'register':
            checkouts = _object(payload.get('checkouts'))
            for host, checkout in checkouts.items():
                if not host or not Path(_registered_path(checkout, host, membership)).is_absolute():
                    raise RoleRefused('checkout mappings must name absolute paths')
            payload = {**payload, 'checkouts': checkouts}
            previous = repositories.get(repository_id)
            if previous is not None and _object(previous).get('checkouts') != checkouts:
                raise RoleRefused('repository already registered; update its checkout mapping explicitly')
            repositories[repository_id] = payload
            _atomic(path, repositories)
            return payload
        repository = repositories.get(repository_id)
        if repository is None:
            host = payload.get('host') or '<host>'
            raise RoleRefused(f'repository {repository_id} is not registered; run design_roles.py register '
                              + f'--repository {repository_id} --host {host} '
                              + '--checkout <absolute path>')
        repository = _object(repository)
        checkouts = _object(repository.get('checkouts'))
        host = _text(payload, 'host')
        if host not in checkouts:
            raise RoleRefused('repository has no explicit checkout mapping for ' + host)
        checkout = _registered_path(checkouts[host], host, membership)
        if operation == 'checkout':
            old = _text(payload, 'old_checkout')
            if checkout not in (old, payload.get('new_checkout')):
                raise RoleRefused('checkout mapping changed')
            previous_checkout = checkouts[host]
            target = _text(payload, 'new_checkout')
            if isinstance(previous_checkout, str):
                checkouts[host] = target
            elif 'configured_checkout' in _object(previous_checkout):
                checkouts[host] = {'configured_checkout': target}
            else:
                checkouts[host] = previous_checkout
            repository['checkouts'] = checkouts
            repositories[repository_id] = repository
            _atomic(path, repositories)
            for placed_path in (root / 'placements').glob('*.json'):
                placed = _read(placed_path)
                if placed.get('repository_id') == repository_id and placed.get('host') == host:
                    _ = placed.pop('checkout', None)
                    _ = placed.pop('repository_owner', None)
                    placed.update(_placement_fields(checkouts[host]))
                    _atomic(placed_path, placed)
            return repository
        document = _text(payload, 'document')
        try:
            relative = (Path(checkout) / document).resolve().relative_to(Path(checkout).resolve())
        except ValueError as error:
            raise RoleRefused('document is outside registered checkout') from error
        if relative == Path('.'):
            raise RoleRefused('document must be inside registered repository')
        key = hashlib.sha256((repository_id + '\0' + relative.as_posix()).encode()).hexdigest()
        placement_path = root / 'placements' / f'{key}.json'
        if placement_path.exists():
            placement = _read(placement_path)
            if placement.get('host') != host:
                raise RoleRefused('design is placed on another host')
            _ = placement.pop('checkout', None)
            _ = placement.pop('repository_owner', None)
            placement.update(_placement_fields(checkouts[host]))
            if operation == 'advance':
                expected = _integer(payload, 'expected_generation')
                if placement.get('generation') not in (expected, expected + 1):
                    raise RoleRefused('authority binding generation changed')
                for alias_path in (root / 'placements').glob('*.json'):
                    alias = _read(alias_path)
                    if alias.get('design_id') == placement.get('design_id'):
                        alias['generation'] = expected + 1
                        _ = alias.pop('checkout', None)
                        _ = alias.pop('repository_owner', None)
                        alias.update(_placement_fields(checkouts[host]))
                        _atomic(alias_path, alias)
                placement['generation'] = expected + 1
            if operation == 'alias':
                try:
                    alias = (Path(checkout) / _text(payload, 'alias')).resolve().relative_to(Path(checkout).resolve()).as_posix()
                except ValueError as error:
                    raise RoleRefused('document alias is outside registered checkout') from error
                alias_key = hashlib.sha256((repository_id + '\0' + alias).encode()).hexdigest()
                alias_path = root / 'placements' / f'{alias_key}.json'
                if alias_path.exists() and _read(alias_path).get('design_id') != placement['design_id']:
                    raise RoleRefused('document alias already belongs to another design')
                _atomic(alias_path, placement)
            return placement
        if operation != 'reserve':
            raise RoleRefused('design placement not recorded')
        placement: dict[str, object] = {'design_id': str(uuid4()), 'host': host, 'generation': 1,
                     'request_id': _text(payload, 'request_id'), 'repository_id': repository_id,
                     'document': relative.as_posix(), **_placement_fields(checkouts[host])}
        _atomic(placement_path, placement)
        return placement


def _check_host(design: HeldDesign) -> None:
    if design.placement.host != local_host():
        raise RoleRefused('design host differs from local host')


def _resolve_legacy_role(design_id: str, instance: RoleInstance, *, local_staff: bool = False) -> RoleInstance:
    """Resolve legacy identity for use in memory."""
    lifecycle = instance.lifecycle
    saved_parent = instance.parent
    if isinstance(lifecycle, RoleNotOpened) and (lifecycle.creation == 'not_started' or not lifecycle.claim):
        if isinstance(saved_parent, RoleIdentityUnresolved):
            return replace(instance, parent=saved_parent.saved_parent)
        return instance
    if isinstance(saved_parent, RoleIdentityUnresolved) and isinstance(saved_parent.saved_parent, RoleParentUnrecorded):
        saved_parent = saved_parent.saved_parent
    if not isinstance(saved_parent, RoleParentUnrecorded) or isinstance(lifecycle, RoleEnded):
        return instance
    record = staff.local_snapshot() if local_staff else staff.snapshot()
    parent: staff.StaffId | RoleIdentityUnresolved
    claim = staff.DesignWorkerClaim(design_id, instance.role)
    if isinstance(lifecycle, RoleNotOpened) and lifecycle.creation == 'outcome_unknown':
        if claim in record.design_workers:
            lifecycle = RoleWorkerPublished(record.design_workers[claim])
    if isinstance(lifecycle, HeldWorker):
        worker = record.by_id(lifecycle.staff_id)
        owner = record.parent_of(worker) if worker is not None else None
        if (worker is None or worker.kind != 'worker' or owner is None
                or isinstance(worker.design_claim, staff.DesignWorkerClaim) and worker.design_claim != claim
                or record.design_workers.get(claim, worker.id) != worker.id):
            parent = RoleIdentityUnresolved('the recorded worker has no proved staff parent', saved_parent)
        else:
            parent = owner.id
    else:
        parent = RoleIdentityUnresolved('the legacy launch has no recorded worker ID', saved_parent)
    updated = replace(instance, parent=parent, lifecycle=lifecycle)
    return replace(updated, name=instance_name(updated, record))


def _require_identity(instance: RoleInstance) -> None:
    if isinstance(instance.parent, RoleIdentityUnresolved):
        raise RoleRefused(f'RoleIdentityUnresolved: {instance.parent.reason}')


def recorded_role(claim: staff.DesignWorkerClaim) -> RoleInstance:
    design = read_design(claim.design_id)
    _check_host(design)
    instance = _resolve_legacy_role(claim.design_id, _instance(design, claim.role))
    _require_identity(instance)
    return instance


def claim_for_worker_name(name: str) -> staff.DesignWorkerClaim | staff.NoDesignWorkerClaim:
    match = re.fullmatch(r'astra-([0-9a-f-]{36})-(designer|reviewer)', name)
    if match is not None:
        claim = staff.DesignWorkerClaim.parse('/'.join(match.groups()))
        if (design_directory(claim.design_id) / 'design.json').exists():
            return claim
    return staff.NO_DESIGN_WORKER_CLAIM


@contextmanager
def start_claim(claim: staff.DesignWorkerClaim) -> Generator[RoleInstance]:
    directory = design_directory(claim.design_id)
    with _lock(directory):
        design = read_design(claim.design_id)
        _check_host(design)
        instance = _instance(design, claim.role)
        _require_no_transition(directory)
        if instance.lifecycle != RoleNotOpened('pane-start', 'outcome_unknown'):
            raise RoleRefused('thread creation claim changed')
        yield instance


def _read_transition(directory: Path) -> dict[str, object]:
    path = directory / 'transition.json'
    return _read(path) if path.exists() else {}


def _require_no_transition(directory: Path) -> None:
    if (directory / 'transition.json').exists():
        raise RoleRefused('RoleNeedsRecovery: a lifecycle transition is incomplete')


def worker_parent(design: HeldDesign, role: Role, record: staff.StaffSnapshot) -> staff.StaffRow:
    """Read the parent of an opened helper from the staff record."""
    instance = _instance(design, role)
    if isinstance(instance.lifecycle, (RoleNotOpened, RoleEnded)):
        raise RoleRefused('role has no active worker')
    row = record.by_id(instance.lifecycle.staff_id)
    parent = record.parent_of(row) if row is not None else None
    if row is None or row.kind != 'worker' or parent is None:
        raise RoleRefused('role worker parent is unavailable')
    return parent


def _validate_binding(binding: Binding, repository: str, role: Role) -> None:
    if role == 'designer' and isinstance(binding, DesignUnbound):
        raise RoleRefused('a designer needs an authorized phase binding')
    if isinstance(binding, DesignBoundToPhase):
        _ = astra_work.phase_work(binding.run_directory, binding.phase)
        if Path(binding_repository(binding)).resolve() != Path(repository).resolve() or not binding.output_paths:
            raise RoleRefused('binding must name its repository and exact output paths')
        for path in binding.output_paths:
            try:
                _ = (Path(repository) / path).resolve().relative_to(Path(repository).resolve())
            except ValueError as error:
                raise RoleRefused('output path escapes repository') from error
        from scripts.direct import phase_reservation
        phases = phase_reservation.open_phases(Path(binding.run_directory))
        name = phase_reservation.phase_directory_name(binding.phase)
        phase = next((phase for phase in phases if phase.phase == name), None)
        if phase is None:
            raise RoleRefused('phase reservation ledger is unreadable or phase is not open')
        _ = _read(phase.directory / 'delegated_phase_reservation_state.json')
        files = (phase.directory / phase_reservation.FILES_FILE).read_text(encoding='utf-8').splitlines()
        allowed = {(Path(repository) / path.strip()).resolve() for path in files if path.strip()}
        if any((Path(repository) / path).resolve() not in allowed for path in binding.output_paths):
            raise RoleRefused('binding paths are outside the phase')


def _idle(design: HeldDesign) -> None:
    if any(instance.assignment_claim or isinstance(instance.lifecycle, RoleWorkerPublished)
           or (isinstance(instance.lifecycle, RoleNotOpened) and instance.lifecycle.claim)
           for instance in design.roles):
        raise RoleRefused("role is busy; an assignment claim is active")


def _pane_recovery_idle(lifecycle: HeldWorker, claim: staff.DesignWorkerClaim) -> None:
    result = _worker_command(['list', 'all', '--json', '--design-role', str(claim)])
    listing = cast(list[object], json.loads(result.stdout))
    fields = next((_object(value) for value in listing if _object(value).get('id') == lifecycle.staff_id),
                  dict[str, object]())
    if fields.get('state') not in ('OpenWorker', 'StoppedWorker', 'ClosedWorker'):
        raise RoleRefused('role process evidence is unavailable; worker is retained')
    if fields.get('state') == 'OpenWorker' and fields.get('activity') not in ('idle', 'done'):
        raise RoleRefused('role is busy; finish its turn first')


def _prompt(design: HeldDesign, role: Role, text: str) -> tuple[Path, Path]:
    directory = design_directory(design.placement.design_id)
    token = f'{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}-{uuid4()}'
    reply = directory / 'replies' / f'{role}-{token}.md'
    prompt = directory / 'prompts' / f'{role}-{token}.md'
    if role == 'designer':
        instructions = (ROOT / 'config/design_roles/designer.md').read_text(encoding='utf-8')
        instructions += '\nUse apply_patch for all repository edits. Edit only the bound output paths.\n'
    else:
        review_prompt = cast(Callable[[Path, str], str],
                             importlib.import_module('scripts.astra.astra_review').review_prompt)
        instructions = review_prompt(Path(design.document), '')
    work = (astra_work.phase_work(design.binding.run_directory, design.binding.phase)
            if isinstance(design.binding, DesignBoundToPhase) else astra_work.StandaloneWork())
    header = astra_work.AstraWork('astra-' + role, instance_name(_instance(design, role)), design.document,
                                 datetime.now(UTC).isoformat(), str(reply), work)
    pending = pending_messages(design.placement.design_id, role)
    message_text = '\n'.join(f'Pending message {message.id} from {message.source}: {message.text}' for message in pending)
    prompt.parent.mkdir(parents=True, exist_ok=True)
    reply.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(prompt, astra_work.header_line(header) + '\n' + instructions + '\n' + text + '\n' + message_text)
    _atomic(prompt.with_suffix('.messages.json'), [message.id for message in pending])
    return prompt, reply


def access_policy(design: HeldDesign, role: Role) -> ThreadAccessPolicy:
    return (PhaseBoundDesigner(design.placement.generation, DESIGNER_SANDBOX)
            if role == 'designer' else ReadOnlyReviewer())


def _worker_command(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    """Use the shared command without another worker selector."""
    return subprocess.run([sys.executable, str(ROOT / 'scripts/production/worker.py'), *arguments],
                          capture_output=True, text=True, check=False)


@dataclass(frozen=True)
class SubmittedRoleTurn:
    """The hook owns completion of this role turn."""

    reply: Path
    run: Path


def submitted_review(reply: Path, run: Path) -> None:
    """Give the caller the read command without retaining a waiting shell."""
    command = [str(ROOT / "scripts/lib/py"), str(ROOT / "scripts/astra/astra_review.py"), "--read", str(reply),
               "--run-directory", str(run)]
    print(f"Submitted: {reply}")
    print("End this turn. Read the review after the hook reports done:")
    print(shlex.join(command))


def dispatch_turn(design: HeldDesign, role: Role, prompt: Path, reply: Path) -> SubmittedRoleTurn:
    """Submit through assign.sh and let the hook save the reply."""
    if isinstance(design.binding, DesignBoundToPhase):
        run, phase = Path(design.binding.run_directory), design.binding.phase
    else:
        run, phase = design_directory(design.placement.design_id), 'astra-review'
        state = worker_lifecycle.read_worker_state(run / 'progress_history_state.json')
        if not state:
            _recorder_command(['start-run', '--session-dir', str(run), '--working-dir', design_repository(design)])
        if phase not in recorder_state.open_phase_ids(state):
            _recorder_command(['start-phase', '--session-dir', str(run), '--phase-id', phase,
                               '--phase-title', 'Held Astra review'])
    instance = _instance(design, role)
    name = instance_name(instance)
    parent = worker_parent(design, role, staff.snapshot())
    result = _assign_command(['--role', 'review' if role == 'reviewer' else 'implement',
        '--target', 'durable-design-role', '--policy', 'astra-' + role,
        '--session-dir', str(run), '--parent', str(parent.id), '--to', name,
        '--phase', phase, '--prompt', str(prompt), '--reply-file', str(reply.resolve()),
        '--completion-actions', json.dumps(asdict(pass_recorder.CompletionActions(
            finish_phase=phase if isinstance(design.binding, DesignUnbound) else '')))])
    refusal = result.stderr.strip() or f'held dispatch exited {result.returncode}; pending input is retained'
    if result.returncode:
        if isinstance(design.binding, DesignUnbound):
            try:
                _recorder_command(['finish-phase', '--session-dir', str(run), '--phase-id', phase,
                                   '--status', 'error'])
            except RoleRefused as error:
                raise RoleRefused(refusal) from error
        raise RoleRefused(refusal)
    return SubmittedRoleTurn(reply, run)


def _assign_command(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(['bash', str(ROOT / 'scripts/direct/assign.sh'), *arguments],
                          capture_output=True, text=True, check=False)


def _recorder_command(arguments: list[str], *, env: Mapping[str, str] = os.environ) -> None:
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/direct/progress_history.py'), *arguments],
                            env=env, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RoleRefused(result.stderr.strip() or 'held dispatch recording did not complete')


def _project_hold(design: HeldDesign, role: Role, record: staff.StaffSnapshot) -> None:
    astra_roles = _census_projection()
    instance = _instance(design, role)
    if isinstance(instance.lifecycle, (RoleNotOpened, RoleWorkerPublished, RoleEnded)):
        return
    _ = worker_parent(design, role, record)
    _ = astra_roles.hold(role, astra_roles.HeldDesignRole(design.placement.design_id, role),
                         str(instance.lifecycle.staff_id))


def refresh_census(design_id: str) -> None:
    design = read_design(design_id)
    record = staff.snapshot()
    for instance in design.roles:
        _project_hold(design, instance.role, record)


def _allow_new_roles() -> None:
    config = configparser.ConfigParser()
    try:
        config.read_string(conf_reads.read_text(
            Path(os.environ.get('DESIGN_ROLES_CONFIG') or ROOT / 'config/design_roles.conf')))
    except FileNotFoundError:
        pass
    if not config.getboolean('roles', 'new_open', fallback=True):
        raise RoleRefused('new role opens and adoptions are disabled; keep existing identities and evidence')


def open_role(repository: str, document: str, role: Role, area: str, *,
              host: str = '', binding: Binding = UNBOUND, request_id: str = '') -> HeldDesign:
    """Reserve identity before creation; retries never make a replacement thread."""
    _allow_new_roles()
    host = host or local_host()
    if host != local_host():
        raise RoleRefused('pair host differs from local host')
    placement = authority_request('reserve', {'repository_id': repository, 'document': document,
                                             'host': host, 'request_id': request_id or str(uuid4())})
    design_id = _text(placement, 'design_id')
    binding = _resolved_binding(binding, _placement_checkout(placement))
    directory = design_directory(design_id)
    with _lock(directory):
        if (directory / 'design.json').exists():
            design = read_design(design_id)
        else:
            checkout = _placement_checkout(placement)
            checkout_source = (UnitCheckout(staff.StaffId(_text(placement, 'repository_owner')))
                               if 'repository_owner' in placement else INDEPENDENT_CHECKOUT)
            binding = _binding_checkout(binding, checkout, checkout_source)
            document_path = str((Path(checkout) / _text(placement, 'document')).resolve())
            design = HeldDesign(DesignPlacement(design_id, host, _integer(placement, 'generation')),
                                repository, checkout, document_path, area, datetime.now(UTC).isoformat(),
                                binding, tuple(RoleInstance(kind, f'astra-{design_id}-{kind}',
                                                          RoleNotOpened()) for kind in ('designer', 'reviewer')),
                                checkout=checkout_source)
            _save(design)
        _check_host(design)
        _require_no_transition(directory)
        instance = _instance(design, role)
        if isinstance(instance.lifecycle, RoleEnded):
            raise RoleRefused('role ended; this design identity cannot be reopened.')
        instance = _resolve_legacy_role(design_id, instance)
        _require_identity(instance)
        if (not isinstance(instance.lifecycle, RoleNotOpened)
                or instance.lifecycle.claim and instance.lifecycle.creation == 'outcome_unknown'):
            design = _set_instance(design, instance)
            if not isinstance(instance.lifecycle, (RoleNotOpened, RoleWorkerPublished)):
                _project_hold(design, role, staff.snapshot())
            else:
                print(f'creation outcome unknown; run recover for {design_id}/{role}; replacement refused', file=sys.stderr)
            return design
        _validate_binding(design.binding, design_repository(design), role)
        parent = instance.parent
        if isinstance(parent, RoleParentUnrecorded):
            caller = staff.snapshot().by_current_session(staff.runtime_id())
            if caller is None or caller.kind not in ('unit', 'showrunner'):
                raise RoleRefused('staff: caller: not opened: caller is not a unit or showrunner')
            parent = caller.id
        name = instance_name(instance)
        design = _set_instance(design, replace(instance, parent=parent,
                                               lifecycle=RoleNotOpened('pane-start', 'not_started')))
        _save(design)
    design = _begin_creation(design, role, 'pane-start')
    run, phase = ((Path(design.binding.run_directory), design.binding.phase) if isinstance(design.binding, DesignBoundToPhase)
                  else (directory, 'astra-review'))
    result = _assign_command(['--role', 'review' if role == 'reviewer' else 'implement',
        '--target', 'durable-design-role', '--policy', 'astra-' + role, '--new-worker', name,
        '--session-dir', str(run), '--phase', phase, '--design-role', str(staff.DesignWorkerClaim(design_id, role))])
    if result.returncode:
        raise RoleRefused(result.stderr.strip() or 'held worker startup did not complete; creation claim is retained')
    with _lock(directory):
        current = read_design(design_id)
        lifecycle = _instance(current, role).lifecycle
        _require_no_transition(directory)
        if not isinstance(lifecycle, RoleWorkerPublished):
            raise RoleRefused('RoleNeedsRecovery: thread creation claim changed')
        record = staff.authority_snapshot()
        if record.by_id(lifecycle.staff_id) is None:
            raise RoleRefused('RoleNeedsRecovery: the recorded worker retired; run end to finish this role')
        _ = worker_parent(current, role, record)
        design = _set_lifecycle(current, role, RoleOpen(lifecycle.staff_id))
        _save(design)
        _project_hold(design, role, record)
    return design


def _begin_creation(design: HeldDesign, role: Role, claim: str) -> HeldDesign:
    """Record an uncertain outcome before the worker command creates a pane."""
    design_id = design.placement.design_id
    directory = design_directory(design_id)
    with _lock(directory):
        _require_no_transition(directory)
        current = read_design(design_id)
        instance = _instance(current, role)
        if (current.placement != design.placement or instance != _instance(design, role)
                or instance.lifecycle != RoleNotOpened(claim, 'not_started')):
            raise RoleRefused('RoleNeedsRecovery: thread creation claim changed')
        updated = _set_lifecycle(current, role, RoleNotOpened(claim, 'outcome_unknown'))
        _save(updated)
        return updated


def recover(instance_id: str) -> HeldDesign:
    """Prove the recorded thread idle before clearing an uncertain local claim."""
    design_id, role_text = instance_id.split('/')
    if role_text not in ('designer', 'reviewer'):
        raise RoleRefused('unknown role')
    role = role_text
    directory = design_directory(design_id)
    claim = staff.DesignWorkerClaim(design_id, role)
    with _lock(directory):
        design = read_design(design_id)
        _check_host(design)
        _require_no_transition(directory)
        saved = _instance(design, role)
        instance = _resolve_legacy_role(design_id, saved)
        _require_identity(instance)
        if isinstance(instance.lifecycle, RoleEnded):
            raise RoleRefused('role ended; this design identity cannot be reopened.')
        lifecycle = instance.lifecycle
        if isinstance(lifecycle, RoleNotOpened):
            if lifecycle.creation == 'not_started' or not lifecycle.claim:
                raise RoleRefused('startup has not started; repeat the original open command')
            raise RoleRefused('RoleNeedsRecovery: no recorded worker; replacement refused; run end to clear the claim')
        if isinstance(lifecycle, RoleWorkerPublished) and staff.authority_snapshot().by_id(lifecycle.staff_id) is None:
            raise RoleRefused('RoleNeedsRecovery: the recorded worker retired; run end to finish this role')
        _pane_recovery_idle(lifecycle, claim)
        observed = _set_instance(design, replace(saved, assignment_claim=''))
        updated = _set_lifecycle(observed, role, RoleOpen(lifecycle.staff_id))
        _save(updated)
        _project_hold(updated, role, staff.snapshot())
        return updated


def resume(instance_id: str, prompt_file: str | Path) -> SubmittedRoleTurn:
    design_id, role_text = instance_id.split('/')
    if role_text not in ('designer', 'reviewer'):
        raise RoleRefused('unknown role')
    role = role_text
    directory = design_directory(design_id)
    observed = _instance(read_design(design_id), role)
    if isinstance(observed.lifecycle, (RoleNeedsRecovery, RoleWorkerPublished)) or observed.assignment_claim:
        _ = recover(instance_id)
    with _lock(directory):
        design = read_design(design_id)
        _check_host(design)
        saved = _instance(design, role)
        instance = _resolve_legacy_role(design_id, saved)
        _require_identity(instance)
        if not isinstance(instance.lifecycle, (RoleOpen, RolePausedForMove)):
            raise RoleRefused(type(instance.lifecycle).__name__ + ': role is unavailable')
        _require_no_transition(directory)
        _idle(design)
        _validate_binding(design.binding, design_repository(design), role)
        generation = design.placement.generation
    prompt, reply = _prompt(design, role, Path(prompt_file).read_text(encoding='utf-8'))
    with _lock(directory):
        current = read_design(design_id)
        if current.placement.generation != generation:
            raise RoleRefused('binding changed before dispatch')
        selected = _instance(current, role)
        _idle(current)
        if selected != saved:
            raise RoleRefused('role changed before dispatch')
        _require_no_transition(directory)
        current = _set_instance(current, replace(selected, assignment_claim=str(prompt)))
        _save(current)

    try:
        submitted = dispatch_turn(design, role, prompt, reply)
    except BaseException as error:
        with _lock(directory):
            current = read_design(design_id)
            selected = _instance(current, role)
            if selected.assignment_claim == str(prompt) and selected.lifecycle == instance.lifecycle:
                failed = replace(selected, assignment_claim='',
                                 lifecycle=RoleNeedsRecovery(instance.lifecycle.staff_id, str(error)))
                _save(_set_instance(current, failed))
        raise
    with _lock(directory):
        current = read_design(design_id)
        selected = _instance(current, role)
        claim_matches = selected.assignment_claim == str(prompt) and selected.lifecycle == instance.lifecycle
        if claim_matches:
            current = _set_instance(current, replace(selected, assignment_claim='',
                                                     lifecycle=RoleOpen(instance.lifecycle.staff_id)))
            _save(current)
        identifiers = cast(list[str], worker_lifecycle.read_worker_value(prompt.with_suffix('.messages.json')))
        for identifier in identifiers:
            (directory / 'pending' / role / f'{identifier}.json').unlink(missing_ok=True)
    if claim_matches:
        _project_hold(current, role, staff.snapshot())
    return submitted


def rebind(design_id: str, *, expected_generation: int, reason: str,
           binding: Binding | KeepBinding = KEEP_BINDING,
           repository: str = '', document: str = '') -> HeldDesign:
    if not reason.strip():
        raise RoleRefused('rebind needs a reason')
    directory = design_directory(design_id)
    design = _load_design(design_id, repair_empty_repository=not isinstance(binding, KeepBinding))
    _check_host(design)
    transition = directory / 'transition.json'
    with _lock(directory):
        fields = _read(directory / 'design.json')
        design = _published_workers(_decode_design(design_id, fields, repair_empty_repository=not isinstance(binding, KeepBinding)))
        damaged_binding = _object(fields.get('binding')).get('repository') == ''
        current_repository = design_repository(design)
        target_repository = repository or current_repository
        same_checkout = Path(target_repository).resolve() == Path(current_repository).resolve()
        checkout = design.checkout
        target_binding = (design.binding if isinstance(binding, KeepBinding) and same_checkout else
                          _binding_checkout(design.binding if isinstance(binding, KeepBinding) else binding,
                                            target_repository, checkout))
        target = replace(design, binding=target_binding,
                         repository=target_repository, checkout=checkout, document=document or design.document)
        # A target must pass the same rules as a saved record before lifecycle effects.
        _ = _decode_design(design_id, _design_fields(target))
        updated = replace(target, placement=replace(target.placement, generation=expected_generation + 1))
        pending = _read(transition) if transition.exists() else {}
        if pending and (pending.get('reason') != reason or pending.get('generation') != expected_generation):
            raise RoleRefused('another lifecycle transition is incomplete')
        committed_retry = bool(pending and design.placement.generation == expected_generation + 1
                               and pending.get('target') == _design_fields(design)
                               and target == design)
        publication_retry = committed_retry or (not damaged_binding and not isinstance(binding, KeepBinding) and target == design and expected_generation >= 1
                             and expected_generation in
                             (design.placement.generation, design.placement.generation - 1))
        if publication_retry:
            if pending and (design.placement.generation != expected_generation + 1
                            or pending.get('target', _design_fields(design)) != _design_fields(design)):
                raise RoleRefused('another lifecycle transition is incomplete')
            if (not pending and {role.lifecycle.staff_id for role in design.roles
                    if not isinstance(role.lifecycle, (RoleNotOpened, RoleEnded))}.issubset(
                        entry['staff_id'] for entry in _census_projection().live_roles(staff.snapshot(),
                            selection=frozenset(role.lifecycle.staff_id for role in design.roles
                                if not isinstance(role.lifecycle, (RoleNotOpened, RoleEnded)))))):
                return design
        else:
            if design.placement.generation != expected_generation:
                raise RoleRefused('stale binding generation')
            _idle(design)
            fields = _design_fields(updated)
            if pending and pending.get('target', fields) != fields:
                raise RoleRefused('another lifecycle transition is incomplete')
            _atomic(transition, {'generation': expected_generation, 'reason': reason, 'target': fields,
                                 'source_binding': _tag(design.binding)})
    if publication_retry:
        return _finish_rebind(design)
    _ = authority_request('advance', {'repository_id': design.repository_id, 'document': design.document,
                                      'host': local_host(), 'expected_generation': expected_generation})
    with _lock(directory):
        current = _load_design(design_id, repair_empty_repository=not isinstance(binding, KeepBinding))
        if current != design:
            raise RoleRefused('binding changed while releasing server')
        _save(updated)
    return _finish_rebind(read_design(design_id))


def _finish_rebind(design: HeldDesign) -> HeldDesign:
    """Keep the transition until the saved generation is fully published."""
    directory = design_directory(design.placement.design_id)
    with _lock(directory):
        if read_design(design.placement.design_id) != design:
            raise RoleRefused('binding changed during publication')
        transition = directory / 'transition.json'
        pending = _read(transition) if transition.exists() else {}
    record = staff.snapshot()
    for instance in design.roles:
        _project_hold(design, instance.role, record)
    with _lock(directory):
        if read_design(design.placement.design_id) != design:
            raise RoleRefused('binding changed during publication')
        if (_read(transition) if transition.exists() else {}) != pending:
            raise RoleRefused('lifecycle transition changed during publication')
        transition.unlink(missing_ok=True)
    return design


def queue_message(design_id: str, role: Role, source: str, text: str, *, message_id: str = '') -> PendingMessage:
    identifier = message_id or str(uuid4())
    _ = UUID(identifier)
    message = PendingMessage(identifier, source, text)
    directory = design_directory(design_id)
    with _lock(directory):
        design = read_design(design_id)
        if isinstance(_instance(design, role).lifecycle, RoleEnded):
            raise RoleRefused('role ended')
        path = directory / 'pending' / role / f'{identifier}.json'
        if path.exists() and _read(path) != asdict(message):
            raise RoleRefused('message id already names other text')
        _atomic(path, asdict(message))
    return message


def pending_messages(design_id: str, role: Role) -> tuple[PendingMessage, ...]:
    return tuple(PendingMessage(_text(value, 'id'), _text(value, 'source'), _text(value, 'text'))
                 for path in sorted((design_directory(design_id) / 'pending' / role).glob('*.json'))
                 for value in [_read(path)])


def end_role(claim: staff.DesignWorkerClaim) -> RoleInstance:
    """Read the role from the record for the saved end transition."""
    directory = design_directory(claim.design_id)
    design = read_design(claim.design_id)
    _check_host(design)
    fence = {'generation': design.placement.generation, 'reason': 'explicit end', 'role': claim.role}
    if _read_transition(directory) != fence:
        raise RoleRefused('the design end claim changed before close')
    instance = _resolve_legacy_role(claim.design_id, _instance(design, claim.role))
    _require_identity(instance)
    if isinstance(instance.lifecycle, RoleEnded):
        raise RoleRefused('the design role already ended')
    return instance


def end(instance_id: str) -> HeldDesign:
    design_id, role_text = instance_id.split('/')
    if role_text not in ('designer', 'reviewer'):
        raise RoleRefused('unknown role: ' + role_text)
    role = role_text
    directory = design_directory(design_id)
    claim = staff.DesignWorkerClaim(design_id, role)
    transition = directory / 'transition.json'
    with _lock(directory):
        design = read_design(design_id)
        _check_host(design)
        instance = _resolve_legacy_role(design_id, _instance(design, role))
        lifecycle = instance.lifecycle
        if not isinstance(lifecycle, RoleEnded):
            _require_identity(instance)
            pending_start = isinstance(lifecycle, (RoleNotOpened, RoleWorkerPublished))
            _idle(_set_instance(design, replace(instance, lifecycle=RoleNotOpened())) if pending_start else design)
            fence = {'generation': design.placement.generation, 'reason': 'explicit end', 'role': role}
            if transition.exists():
                if _read(transition) != fence:
                    raise RoleRefused('another lifecycle transition is incomplete')
            else:
                _atomic(transition, fence)
            if isinstance(lifecycle, RoleNotOpened) and lifecycle.creation == 'outcome_unknown' and lifecycle.claim:
                staff.seal_design_worker_start(claim)
                design = read_design(design_id)
                lifecycle = _instance(design, role).lifecycle

    if not isinstance(lifecycle, RoleEnded):
        if isinstance(lifecycle, HeldWorker):
            result = _worker_command(['end', '--design-role', str(claim)])
            if result.returncode:
                raise RoleRefused(result.stderr.strip() or f'held worker end exited {result.returncode}; retry end')
        reference = lifecycle.staff_id if isinstance(lifecycle, HeldWorker) else staff.StaffId('')
        with _lock(directory):
            current = read_design(design_id)
            design = _set_lifecycle(current, role, RoleEnded(reference))
            _save(design)
    _finish_end(design, role)
    return design


def _finish_end(design: HeldDesign, role: Role) -> None:
    """Release the census before clearing this role's end fence."""
    lifecycle = _instance(design, role).lifecycle
    if isinstance(lifecycle, HeldWorker) and lifecycle.staff_id:
        _census_projection().release(lifecycle.staff_id)
    directory = design_directory(design.placement.design_id)
    with _lock(directory):
        if isinstance(design.binding, DesignUnbound):
            if unit_runs.run_is_open(directory):
                _recorder_command(['finish-run', '--session-dir', str(directory), '--status', 'completed'])
        transition = directory / 'transition.json'
        pending = _read(transition) if transition.exists() else {}
        if pending.get('reason') == 'explicit end' and pending.get('role') == role:
            transition.unlink(missing_ok=True)


def list_roles() -> dict[str, object]:
    entries: list[dict[str, object]] = []
    problems: list[dict[str, str]] = []
    for path in sorted(state_root().glob('*/design.json')):
        try:
            design = _load_design(path.parent.name, repair_empty_repository=False, local_staff=True)
            for saved in design.roles:
                role = _resolve_legacy_role(design.placement.design_id, saved, local_staff=True)
                lifecycle = role.lifecycle
                pending = _read_transition(path.parent)
                if pending and not isinstance(lifecycle, (RoleNotOpened, RoleEnded)):
                    lifecycle = RoleNeedsRecovery(lifecycle.staff_id, 'incomplete ' + str(pending.get('reason')))
                state = (_tag(lifecycle) if not isinstance(role.parent, RoleIdentityUnresolved)
                         else {'kind': 'RoleIdentityUnresolved', 'reason': role.parent.reason})
                entries.append({'design_id': design.placement.design_id, 'instance_id': design.placement.design_id + '/' + role.role,
                                'role': role.role, 'name': instance_name(role), 'lifecycle': state,
                                'binding': _tag(design.binding), 'generation': design.placement.generation,
                                'document': design.document, 'host': design.placement.host})
        except (OSError, ValueError, KeyError, TypeError) as error:
            problems.append({'path': str(path), 'reason': str(error)})
    return {'roles': entries, 'problems': problems}


def render_roles(report: dict[str, object]) -> str:
    lines: list[str] = []
    for entry in cast(list[dict[str, object]], report['roles']):
        lifecycle = _object(entry['lifecycle'])
        thread = ' | staff ' + _text(lifecycle, 'staff_id') if lifecycle.get('staff_id') else ''
        reason = ' | ' + _text(lifecycle, 'reason') if lifecycle.get('kind') == 'RoleIdentityUnresolved' else ''
        lines.append(f"{entry['instance_id']} | {entry['role']} | {_text(lifecycle, 'kind')} | "
                     + f"{thread} | {entry['document']}{reason}")
    return '\n'.join(lines) if lines else 'no held roles'


def _resolved_binding(binding: Binding, repository: str) -> Binding:
    if isinstance(binding, DesignBoundToPhase) and not binding_repository(binding):
        return replace(binding, repository=repository)
    return binding


def _has_live_roles(fields: dict[str, object]) -> bool:
    roles = fields.get('roles')
    if not isinstance(roles, list):
        return False
    return any(isinstance(value, dict)
               and isinstance(lifecycle := cast(dict[str, object], value).get('lifecycle'), dict)
               and cast(dict[str, object], lifecycle).get('kind') in (
                   'RoleOpen', 'RolePausedForMove', 'RoleNeedsRecovery', 'RoleWorkerPublished')
               for value in cast(list[object], roles))


@dataclass
class _RecordWarnings:
    paths: set[Path]


_record_warnings: ContextVar[_RecordWarnings] = ContextVar('design_role_record_warnings')


@contextmanager
def record_operation() -> Generator[None]:
    """Report each bad record once across this operation's nested sweeps."""
    try:
        _ = _record_warnings.get()
    except LookupError:
        token = _record_warnings.set(_RecordWarnings(set()))
        try:
            yield
        finally:
            _record_warnings.reset(token)
    else:
        yield


def _warn_design_record(path: Path, reason: str) -> None:
    warned = _record_warnings.get().paths
    if path not in warned:
        print(f'warning: {path}: {reason}', file=sys.stderr)
        warned.add(path)


def _read_designs(relevant: Callable[[Path, dict[str, object]], bool], *,
                  refuse_matching_damage: bool = False, audit_checkout_paths: bool = False) -> Generator[HeldDesign]:
    """Skip broken records, but preserve a caller's matching live role for recovery."""
    with record_operation():
        for path in sorted(state_root().glob('*/design.json')):
            fields: dict[str, object] = {}
            try:
                fields = _read(path)
                resolve = _parse_design(path.parent.name, fields)
            except (OSError, ValueError, KeyError, TypeError) as error:
                reason = ' '.join(str(error).splitlines())
                live_roles = _has_live_roles(fields)
                if fields and (refuse_matching_damage or live_roles) and relevant(path, fields):
                    recovery = '; live held role needs recovery' if live_roles else ''
                    raise RoleRefused(f'{path}: {reason}{recovery}') from error
                _warn_design_record(path, reason)
                continue
            if audit_checkout_paths:
                yield resolve()
            elif relevant(path, fields):
                yield _published_workers(resolve())


@dataclass(frozen=True)
class NotHeldDocument:
    pass


def held_document(document: Path) -> HeldDesign | NotHeldDocument:
    target = str(document.resolve())

    def matches_owned_document(fields: dict[str, object]) -> bool:
        try:
            return str((Path(_read_checkout(fields)[0]) / _text(fields, 'document')).resolve()) == target
        except RoleRefused:
            return False

    def relevant(path: Path, fields: dict[str, object]) -> bool:
        if 'repository_owner' not in fields:
            return fields.get('document') == target
        try:
            return matches_owned_document(fields)
        except staff.StaffUnreadable as error:
            # Check the checkout again before skipping the record.
            if matches_owned_document(fields):
                raise
            _warn_design_record(path, ' '.join(str(error).splitlines()))
            return False

    found = [design for design in _read_designs(relevant) if design.document == target]
    if len(found) > 1:
        raise RoleRefused('document has more than one held identity; recovery required')
    return found[0] if found else NotHeldDocument()


def unbind_run(run_directory: str) -> None:
    def matches_run(binding: object) -> bool:
        if not isinstance(binding, dict):
            return False
        directory = cast(dict[str, object], binding).get('run_directory')
        return isinstance(directory, str) and Path(directory) == Path(run_directory)

    def relevant(path: Path, fields: dict[str, object]) -> bool:
        return (matches_run(fields.get('binding'))
                or matches_run(_read_transition(path.parent).get('source_binding')))

    for design in _read_designs(relevant, refuse_matching_damage=True):
        pending = _read_transition(design_directory(design.placement.design_id))
        source_binding = pending.get('source_binding')
        if (matches_run(source_binding)
                and pending.get('target') == _design_fields(design)):
            _ = _finish_rebind(design)
        if matches_run(_tag(design.binding)):
            _ = rebind(design.placement.design_id, expected_generation=design.placement.generation,
                       reason='run ended after reservation release', binding=DesignUnbound())


def check_patch(design_id: str, generation: int, cwd: Path, paths: list[str]) -> DesignBoundToPhase:
    """Reject the whole patch before attribution, including symlink and rename escapes."""
    design = read_design(design_id)
    if (design_directory(design_id) / 'transition.json').exists():
        raise RoleRefused('binding transition is incomplete')
    if design.placement.generation != generation:
        raise RoleRefused('stale binding generation')
    _check_host(design)
    if not isinstance(design.binding, DesignBoundToPhase):
        raise RoleRefused('designer is unbound')
    binding = design.binding
    _validate_binding(binding, design_repository(design), 'designer')
    root = Path(binding_repository(binding)).resolve()
    allowed = {(root / name).resolve() for name in binding.output_paths}
    for path in paths:
        candidate = (cwd / path).resolve()
        if candidate not in allowed:
            raise RoleRefused('unlisted or escaping output path: ' + path)
        try:
            _ = candidate.relative_to(root)
        except ValueError as error:
            raise RoleRefused('output path escapes repository: ' + path) from error
    from scripts.direct import phase_reservation
    ledger = phase_reservation.phase_directory(Path(binding.run_directory), binding.phase) / phase_reservation.STATE_FILE
    _ = _read(ledger)
    return binding


def move_checkout(old_checkout: str, new_checkout: str, *,
                reason: str) -> None:
    """Move live document paths and preserve saved prompts and replies."""
    if not old_checkout or old_checkout == new_checkout:
        return

    def moved(value: str) -> str:
        try:
            return str(Path(new_checkout) / Path(value).relative_to(old_checkout))
        except ValueError:
            return value

    for design in _read_designs(lambda _path, fields: (
            _read_checkout(fields)[0] if 'repository_owner' in fields else fields.get('repository')) in (old_checkout, new_checkout),
            refuse_matching_damage=True):
        if design_repository(design) not in (old_checkout, new_checkout):
            continue
        directory = design_directory(design.placement.design_id)
        transition = directory / 'transition.json'
        pending = _read(transition) if transition.exists() else {}
        request = {'old_checkout': old_checkout, 'new_checkout': new_checkout, 'reason': reason}
        if pending:
            if any(pending.get(key) != value for key, value in request.items()):
                raise RoleRefused('another lifecycle transition is incomplete')
            fields = _object(pending.get('target'))
            if fields == _design_fields(design):
                _ = _finish_rebind(design)
                continue
            updated = _decode_design(design.placement.design_id, fields)
            if (design.placement.generation != _integer(pending, 'generation')
                    or updated.placement.generation != design.placement.generation + 1
                    or updated.checkout != design.checkout or updated.repository_id != design.repository_id
                    or updated.placement.host != design.placement.host):
                raise RoleRefused('checkout transition differs from the saved design')
        else:
            if design_repository(design) == new_checkout and design.document == moved(design.document):
                continue
            binding = design.binding
            if isinstance(binding, DesignBoundToPhase):
                binding = replace(binding, repository=moved(binding_repository(binding)), run_directory=moved(binding.run_directory),
                                  output_paths=tuple(moved(value) for value in binding.output_paths))
            checkout = design.checkout
            updated = replace(design, repository=new_checkout, checkout=checkout, document=moved(design.document),
                              binding=_binding_checkout(binding, new_checkout, checkout),
                              placement=replace(design.placement, generation=design.placement.generation + 1))
            fields = _design_fields(updated)
        with _lock(directory):
            if read_design(design.placement.design_id) != design:
                raise RoleRefused(f'binding changed before {reason}')
            _idle(design)
            if (_read(transition) if transition.exists() else {}) != pending:
                raise RoleRefused('checkout transition changed before publication')
            if not pending:
                _atomic(transition, {**request, 'generation': design.placement.generation, 'target': fields, 'reset_routes': True})
        _ = authority_request('checkout', {'repository_id': design.repository_id, 'host': local_host(),
                                          'old_checkout': old_checkout, 'new_checkout': new_checkout})
        _ = authority_request('advance', {'repository_id': design.repository_id,
                                         'document': Path(updated.document).relative_to(updated.repository).as_posix(),
                                         'host': local_host(), 'expected_generation': design.placement.generation})
        with _lock(directory):
            if read_design(design.placement.design_id) != design:
                raise RoleRefused(f'binding changed during {reason}')
            _save(updated)
        _ = _finish_rebind(read_design(design.placement.design_id))


def checkout_failures(old_checkout: str) -> list[str]:
    """List live document paths that still use the old checkout."""
    failures: list[str] = []
    try:
        for design in _read_designs(lambda _path, fields: (
                _read_checkout(fields)[0] if 'repository_owner' in fields else fields.get('repository')) == old_checkout,
                audit_checkout_paths=True):
            path = design_directory(design.placement.design_id) / 'design.json'
            try:
                if old_checkout:
                    values = [design_repository(design), design.document]
                    if isinstance(design.binding, DesignBoundToPhase):
                        values.extend((binding_repository(design.binding), design.binding.run_directory,
                                       *design.binding.output_paths))
                    if any(value == old_checkout or value.startswith(old_checkout + '/') for value in values):
                        failures.append(str(path) + ': live binding still names old checkout')
            except (OSError, ValueError) as error:
                failures.append(str(path) + ': ' + str(error))
    except RoleRefused as error:
        failures.append(str(error))
    return failures


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('open')
    for flag in ('repository', 'document', 'role', 'area'):
        _ = p.add_argument('--' + flag, required=True)
    _ = p.add_argument('--host', default='')
    _ = p.add_argument('--run-directory', default='')
    _ = p.add_argument('--phase', default='')
    _ = p.add_argument('--output-path', action='append', default=[])
    _ = p.add_argument('--checkout', default='')
    p = commands.add_parser('resume')
    _ = p.add_argument('instance_id')
    _ = p.add_argument('--prompt-file', required=True)
    p = commands.add_parser('end')
    _ = p.add_argument('instance_id')
    p = commands.add_parser('recover')
    _ = p.add_argument('instance_id')
    p = commands.add_parser('rebind')
    _ = p.add_argument('design_id')
    _ = p.add_argument('--expected-generation', type=int, required=True)
    _ = p.add_argument('--reason', required=True)
    _ = p.add_argument('--unbind', action='store_true')
    for flag in ('run-directory', 'phase', 'checkout', 'document'):
        _ = p.add_argument('--' + flag, default='')
    _ = p.add_argument('--output-path', action='append', default=[])
    p = commands.add_parser('unbind-run')
    _ = p.add_argument('run_directory')
    p = commands.add_parser('list')
    _ = p.add_argument('--json', action='store_true')
    p = commands.add_parser('authority')
    _ = p.add_argument('operation')
    p = commands.add_parser('register')
    _ = p.add_argument('--repository', required=True)
    _ = p.add_argument('--host', required=True)
    location = p.add_mutually_exclusive_group(required=True)
    _ = location.add_argument('--checkout', default='')
    _ = location.add_argument('--repository-owner', default='')
    args = cast(dict[str, object], vars(parser.parse_args(argv)))
    def argument(key: str) -> str:
        return cast(str, args.get(key, ''))
    try:
        command = argument('command')
        if command == 'open':
            role = argument('role')
            if role not in ('designer', 'reviewer'):
                raise RoleRefused('unknown role')
            binding: Binding = UNBOUND
            if argument('run_directory') or argument('phase'):
                if not argument('run_directory') or not argument('phase'):
                    raise RoleRefused('run-directory and phase are required together')
                binding = DesignBoundToPhase(str(Path(argument('run_directory')).resolve()), argument('phase'),
                                             argument('checkout'), tuple(cast(list[str], args['output_path'])))
            design = open_role(argument('repository'), argument('document'), role, argument('area'),
                               host=argument('host'), binding=binding)
            print(design.placement.design_id + '/' + role)
        elif command == 'resume':
            submitted = resume(argument('instance_id'), argument('prompt_file'))
            submitted_review(submitted.reply, submitted.run)
        elif command == 'rebind':
            repair = bool(args.get('unbind') or (argument('run_directory') and argument('phase')
                                                and argument('checkout')))
            current = _load_design(argument('design_id'), repair_empty_repository=repair)
            next_binding = current.binding
            if args.get('unbind'):
                next_binding = DesignUnbound()
            elif argument('run_directory') or argument('phase'):
                if not argument('run_directory') or not argument('phase'):
                    raise RoleRefused('run-directory and phase are required together')
                next_binding = DesignBoundToPhase(str(Path(argument('run_directory')).resolve()), argument('phase'),
                                                 argument('checkout') or design_repository(current),
                                                 tuple(cast(list[str], args['output_path'])) or
                                                 (current.binding.output_paths if isinstance(current.binding, DesignBoundToPhase) else ()))
            updated = rebind(argument('design_id'), expected_generation=cast(int, args['expected_generation']),
                       reason=argument('reason'), binding=next_binding,
                       repository=argument('checkout'), document=argument('document'))
            print(f'{updated.placement.design_id} rebound at generation {updated.placement.generation}')
        elif command == 'end':
            _ = end(argument('instance_id'))
        elif command == 'recover':
            _ = recover(argument('instance_id'))
        elif command == 'unbind-run':
            unbind_run(argument('run_directory'))
        elif command == 'register':
            checkout = (UnitCheckout(staff.StaffId(argument('repository_owner')))
                        if argument('repository_owner') else argument('checkout'))
            register_repository(argument('repository'), {argument('host'): checkout})
        elif command == 'authority':
            config = configparser.ConfigParser()
            try:
                config.read_string(conf_reads.read_text(
                    Path(os.environ.get('DESIGN_ROLES_CONFIG') or ROOT / 'config/design_roles.conf')))
            except FileNotFoundError:
                pass
            if config.get('ownership', 'host') != local_host():
                raise RoleRefused('this host is not the ownership authority')
            print(json.dumps(_authority_local(argument('operation'), _object(cast(object, json.load(sys.stdin))))))
        else:
            report = list_roles()
            for problem in cast(list[dict[str, str]], report['problems']) if not args.get('json') else ():
                print(f"problem: {problem['path']}: {problem['reason']}", file=sys.stderr)
            print(json.dumps(report) if args.get('json') else render_roles(report))
    except recorder_state.OldRecorderState as error:
        print(error, file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, configparser.Error) as error:
        damaged = (error.__cause__ if isinstance(error, worker_lifecycle.WorkerStateUnreadable)
                   else error)
        detail = (f'{damaged.path}: missing or invalid {damaged.field}'
                  if isinstance(damaged, DamagedDesignRecord) else str(error))
        print('design_roles: ' + detail.replace('\n', ' '), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
