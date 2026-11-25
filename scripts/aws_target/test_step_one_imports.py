"""Check cold imports in Hana placement."""
from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from inspect import currentframe
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import FrameType
import unittest
from unittest import mock

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.build_queue import admission_protocol as wire, placement


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPTS = REPOSITORY / 'scripts'
FIXTURE_REPOSITORY = 'https://example.com/fixture'

# The GPU sequence in open1c/import_sweep_reply_1.txt has 67 source rows.
# List each module once. Several modules account for more than one row.
GPU_MODULES = frozenset('scripts.' + module for module in """
    agents.remote
    aws_target.aws_target aws_target.cloud_gate aws_target.gate_box aws_target.gpu_size_trial
    aws_target.hana_spend
    build_hold.build_hold build_hold.override_guard
    build_queue.admission_client build_queue.program_start build_queue.queue_place
    build_targets.target_config
    buildlog.record buildlog.step_context
    copies.copies
    direct.pass_record direct.phase_table direct.progress_history
    lib.build_copies lib.build_folders lib.conf lib.conf_reads lib.land_main lib.scratch
    lib.unit_runs lib.user_zone lib.waiting_rows
    lint.memory_admit lint.sweep
    mac_test.floor_claims mac_test.mac_test
    message.process_table message.session_send message.sessions
    production.change_review production.gate production.record_store production.session_staff
    production.showrunners production.staff production.unit_config production.unit_lookup
    whoami.note_file worktree.worktrees
""".split()) | {'_devtools_python_startup'}
HANA_MODULES = frozenset(('scripts.aws_target.hana_box', 'scripts.aws_target.hana_execution',
                         'scripts.mac_test.mac_test'))
ADAPTER_MODULE = 'scripts.aws_target.hana_placement_adapter'


def observe_file_opens(report: Callable[[Path, str], None]) -> None:
    """Report file opens and identify the caller of package_root.py."""
    def audit(event: str, args: tuple[object, ...]) -> None:
        if event in ('socket.connect', 'subprocess.Popen'):
            raise AssertionError(f'Unexpected external operation: {event}')
        if event == 'open' and isinstance(args[0], str):
            path = Path(os.path.abspath(args[0]))
            module = ''
            if path == SCRIPTS / 'lib/package_root.py':
                frame = currentframe()
                caller = frame.f_back if frame is not None else None
                del frame
                if caller is not None:
                    name: object = caller.f_globals.get('__name__')
                    if isinstance(name, str):
                        module = name
            report(path, module)

    sys.addaudithook(audit)


def evidence_request(root: Path, target: str, kind: placement.CommandKind, command: tuple[str, ...]
                     ) -> tuple[placement.HostEvidenceKey, wire.PlacementRequest]:
    from scripts.build_hold.owning_session import OwningSession
    from scripts.build_queue import admission_protocol as wire, admission_records as records, execution_identity, placement
    from scripts.mac_test.claim_state import RepositoryScope

    session = OwningSession('fixture-session')
    destination = placement.PlacementDestination('ssh', target, 'fixture-host',
        RepositoryScope('fixture', FIXTURE_REPOSITORY), 1)
    key = placement.HostEvidenceKey(destination, session, command, 'fixture',
        repository=str(root), package='fixture', command_kind=kind)
    constraints = placement.PlacementConstraints(execution_identity.SourceKeyKnown('fixture-source'),
        command, str(root), key.package, session, (placement.PlacementCandidate(destination),),
        placement.PinnedHost(target), command_kind=kind)
    profile = records.StepProfile(key.package, kind, str(root), records.ExpectedPeak(1024, 'fallback', 0))
    request = wire.PlacementRequest('fixture-episode', placement.SubmittingOwner('fixture', 1, 'start'),
        0, profile, constraints)
    return key, request


def check_gpu_import_order(root: Path, valid_selection: bool) -> None:
    from scripts.aws_target import hana_placement_adapter as adapter
    from scripts.build_queue import placement
    from scripts.build_targets.execution_transport import HanaGpuNextestTransport
    from scripts.lib import conf
    from scripts.mac_test import eligibility

    settings = root / 'cloud_gate.conf'
    selection = 'fixture:gpu_case' if valid_selection else ''
    _ = settings.write_text(f'enabled=off\nhana_gpu_repository={FIXTURE_REPOSITORY}\nhana_gpu_selections={selection}\n')
    _ = (root / 'build_targets.conf').write_text('targets=hana-gpu\nkind.hana-gpu=ssh\nplatform.hana-gpu=linux\n'
                                                + 'settings.hana-gpu=cloud_gate.conf\n')
    command = ('cargo', 'nextest', 'run', '--no-fail-fast', '--workspace', '--test', 'gpu_case', '-E', 'package(fixture)')
    key, request = evidence_request(root, 'hana-gpu', 'test', command)
    transport = HanaGpuNextestTransport(settings, key.destination.host)
    assert 'scripts.aws_target.gpu_size_trial' not in sys.modules, 'The GPU trial import must be cold'
    startup_read = False
    policy_reads: list[tuple[bool, list[str]]] = []

    def observe(path: Path, module: str) -> None:
        nonlocal startup_read
        if module == 'scripts.aws_target.gpu_size_trial':
            startup_read = True
        if path == settings and not policy_reads:
            policy_reads.append((startup_read, sorted(GPU_MODULES.difference(sys.modules))))

    observe_file_opens(observe)
    with (mock.patch.object(eligibility, 'repository_identity',
                            return_value=eligibility.KnownRepository('fixture', FIXTURE_REPOSITORY)),
          mock.patch.object(conf, '_committed_files', return_value=())):
        try:
            result = adapter.read_gpu_evidence(transport, key, request)
        except conf.Unusable as error:
            assert not valid_selection and 'hana_gpu_selections' in str(error), error
        else:
            assert valid_selection, 'The blank GPU selection must raise conf.Unusable'
            assert result.eligibility == placement.FixedExclusion('cloud gates are off (cloud_gate.conf enabled=off)'), result

    assert policy_reads, 'The request must open the GPU policy file'
    startup_before_policy, missing = policy_reads[0]
    assert startup_before_policy and not missing, (
        f'GPU startup read before policy: {startup_before_policy}. Missing GPU modules at first policy read: {missing}.')


def check_hana_import_order(root: Path, route_on: bool) -> None:
    from scripts.aws_target import hana_placement
    from scripts.build_queue import placement
    from scripts.build_targets.execution_transport import HanaControlTransport
    from scripts.lib import conf
    from scripts.mac_test import eligibility

    settings = root / 'hana_aws.conf'
    setting = 'on' if route_on else 'off'
    _ = settings.write_text(f'enabled={setting}\nrepository_identity={FIXTURE_REPOSITORY}\n'
                            + 'kinds=check\n# whole: 1..1\nmax_steps=1\n# whole: 1..\nfree_floor_gib=30\n')
    workload = conf.read(settings)
    key, request = evidence_request(root, 'hana', 'check', ('cargo', 'check', '--fix'))
    transport = HanaControlTransport(settings, key.destination.host)
    assert HANA_MODULES.isdisjoint(sys.modules), 'The Hana and Mac imports must be cold'
    startup_read = False
    clock_reads: list[tuple[bool, list[str]]] = []

    def observe(_path: Path, module: str) -> None:
        nonlocal startup_read
        if module == 'scripts.mac_test.mac_test':
            startup_read = True

    def observe_clock(frame: FrameType, event: str, function: object) -> None:
        if (event == 'c_call' and function is time.time and frame.f_code.co_name == 'read_evidence'
                and frame.f_globals.get('__name__') == ADAPTER_MODULE
                and not clock_reads):
            clock_reads.append((startup_read, sorted(HANA_MODULES.difference(sys.modules))))

    observe_file_opens(observe)
    with mock.patch.object(eligibility, 'repository_identity',
                           return_value=eligibility.KnownRepository('fixture', FIXTURE_REPOSITORY)):
        sys.setprofile(observe_clock)
        try:
            result = hana_placement.read_evidence(key, request, transport=transport, workload=workload)
        finally:
            sys.setprofile(None)
    assert isinstance(result, placement.FreshHostEvidence), result
    assert result.eligibility == placement.FixedExclusion('command' if route_on else 'off'), result.eligibility
    assert clock_reads, 'The request must read the Hana clock'
    startup_before_clock, missing = clock_reads[0]
    assert startup_before_clock and not missing, (
        f'Mac startup read before clock: {startup_before_clock}. Missing Hana modules at first clock read: {missing}.')


def check_adapter_cache(root: Path) -> None:
    import dataclasses
    from scripts.lib.python_startup import sitecustomize as startup

    # Load the adapter's dependencies before enabling the cache.
    _ = import_module('scripts.aws_target.hana_box')
    _ = import_module('scripts.aws_target.hana_placement')
    _ = import_module('scripts.build_queue.execution_identity')
    assert ADAPTER_MODULE not in sys.modules, 'The adapter import must be cold'
    cache = root / 'methods'
    cache.mkdir(mode=0o700)

    with (mock.patch.object(startup, 'dataclass_code_root', return_value=str(cache)),
          mock.patch.object(dataclasses, 'exec', startup.dataclass_exec, create=True)):
        _ = import_module(ADAPTER_MODULE)
        entries = list(cache.rglob('*'))
        assert not entries, f'The cold adapter import created {sum(path.is_file() for path in entries)} cache entries.'

        @dataclasses.dataclass
        class CacheProbe:
            value: int

        assert CacheProbe(1).value == 1
        assert any(path.is_file() for path in cache.rglob('*')), 'The dataclass cache must accept generated methods'


class StepOneImportTests(unittest.TestCase):
    def run_cold(self, check: Callable[..., None], *args: bool) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        home = root / 'home'
        home.mkdir()
        state = root / 'state'
        state.mkdir()
        code = f'from scripts.aws_target.test_step_one_imports import {check.__name__}\n' + (
            f'from pathlib import Path\nimport sys\n{check.__name__}(Path(sys.argv[1]), *{args!r})')
        result = subprocess.run([sys.executable, '-B', '-S', '-c', code, str(root)], cwd=REPOSITORY,
            env={'HOME': str(home), 'CONF_DIR': str(root), 'XDG_CONFIG_HOME': str(home / '.config'),
                 'XDG_STATE_HOME': str(state), 'XDG_CACHE_HOME': str(root / 'cache'),
                 'XDG_RUNTIME_DIR': str(root), 'TMPDIR': str(root), 'BUILD_TARGETS_STATE_DIR': str(state),
                 'MAC_TEST_STATE_DIR': str(state / 'mac')},
            capture_output=True, text=True, timeout=120, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_gpu_imports_precede_policy_read(self) -> None:
        self.run_cold(check_gpu_import_order, True)

    def test_gpu_imports_precede_policy_read_with_invalid_selection(self) -> None:
        self.run_cold(check_gpu_import_order, False)

    def test_hana_imports_precede_clock_when_route_is_off(self) -> None:
        self.run_cold(check_hana_import_order, False)

    def test_hana_imports_precede_clock_when_command_is_refused(self) -> None:
        self.run_cold(check_hana_import_order, True)

    def test_cold_adapter_import_leaves_the_method_cache_empty(self) -> None:
        self.run_cold(check_adapter_cache)
