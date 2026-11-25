"""Run the common trial and production guest on a disposable gate instance."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
import inspect
import json
import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast
from enum import StrEnum
from hashlib import file_digest
import math
import subprocess

from scripts.lib.python_startup import sitecustomize as python_startup
from scripts.production.gate_routes import GateJobKind, GateJobResult, GateRun, SuiteMode

if TYPE_CHECKING:
    from scripts.test_times import gate_record



class TrialVariant(StrEnum):
    BASE = 'base'
    CHANGED = 'changed'


RUN_FIELDS = {'module', 'variant', 'round', 'cpu_s', 'peak_mib', 'wall_s', 'status', 'tests',
              'failures', 'errors', 'instance_type', 'box'}


def validate_paired_result(row: dict[str, object], request: dict[str, object], instance: str) -> None:
    """Validate the task identity and each measured sample."""
    from scripts.aws_target import ledger
    for key in ('attempt', 'commit', 'tree', 'base', 'changed', 'module', 'rounds'):
        if row.get(key) != request[key]:
            raise ValueError('The paired result differs from ' + key)
    seen: set[tuple[str, int]] = set()
    for sample in ledger.rows(row, 'runs'):
        if set(sample) != RUN_FIELDS or sample['module'] != request['module']:
            raise ValueError('The paired sample has different fields or module')
        variant = TrialVariant(ledger.text(sample, 'variant'))
        number = ledger.instant(sample, 'round')
        if not 1 <= number <= ledger.instant(request, 'rounds') or (variant, number) in seen:
            raise ValueError('The paired sample has a duplicate or invalid round')
        seen.add((variant, number))
        if type(sample['status']) is not int:
            raise ValueError('The paired status must be an integer')
        for field in ('cpu_s', 'peak_mib', 'wall_s', 'tests', 'failures', 'errors'):
            value = sample[field]
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError('The paired sample has an invalid ' + field)
            if field in ('tests', 'failures', 'errors') and type(value) is not int:
                raise ValueError('The paired count must be an integer')
        if sample['box'] not in ('', instance):
            raise ValueError('The paired sample has a different box')
        sample['box'] = instance
    if row.get('verdict') not in ('pass', 'fail', 'error'):
        raise ValueError('The paired result has no verdict')


# The candidate supplies the supervisor for both tested trees.
PAIRED_GUEST = r'''from pathlib import Path
import math, signal, tempfile
from scripts.production.gate import process_memory_mib, test_module_environment


def paired_trial():
    rows = []
    reply = {key: gate_request[key] for key in ('attempt', 'commit', 'tree', 'base', 'changed', 'module', 'rounds')}
    reply.update(timestamps, runs=rows, gate_s=0, verdict='error')
    started = time.monotonic()
    active = None
    adopt_descendants()
    log = root / 'gate.log'
    def upload():
        put('log', b''.join(path.read_bytes() for path in (root / 'boot.log', log, root / 'timeline.log') if path.exists()))
    try:
        trees = {variant: root / variant for variant in ('base', 'changed')}
        (root / 'measure').touch()
        with (root / 'upload.lock').open('a') as lock:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
            for round_number in range(gate_request['rounds'] + 1):
                warming = round_number == 0
                variants = ('base', 'changed') if warming or round_number % 2 else ('changed', 'base')
                remaining = gate_request['expires'] - time.time() - gate_request['delivery_s']
                budget = min(gate_request['module_time_limit_s'], max(0, remaining / (2 * (gate_request['rounds'] - round_number + 1))))
                for variant in variants:
                    stage('warm-up ' + variant if warming else f"round {round_number} of {gate_request['rounds']} {variant}")
                    upload()
                    repo = trees[variant]
                    expected = root.absolute() / variant
                    if repo.absolute() != expected or repo.resolve() != expected:
                        raise ValueError(f"Cannot reset {repo}: expected the checkout at {expected} without links")
                    top = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--show-toplevel'], capture_output=True)
                    if top.returncode or os.fsdecode(top.stdout.removesuffix(b'\n')) != str(expected):
                        raise ValueError(f"Cannot reset {repo}: Git must report {expected} as the working tree root")
                    subprocess.run(['git', '-C', str(repo), 'reset', '--hard'], check=True, capture_output=True)
                    subprocess.run(['git', '-C', str(repo), 'clean', '-fdx', '-e', '__pycache__/'], check=True, capture_output=True)
                    with tempfile.TemporaryDirectory(dir=root) as temporary:
                        work = Path(temporary)
                        environment = test_module_environment(work, dict(os.environ))
                        if warming:
                            environment.pop('TEST_TIMES_OUT', None)
                        environment['CONF_DIR'] = str(repo / 'config')
                        home = work / 'home'
                        (home / '.claude').mkdir(parents=True)
                        (home / '.claude/config').symlink_to(repo / 'config', target_is_directory=True)
                        (home / '.claude/scripts').symlink_to(repo / 'scripts', target_is_directory=True)
                        environment['HOME'] = str(home)
                        folder, name = gate_request['module'].rsplit('/', 1)
                        row = dict(module=gate_request['module'], variant=variant, round=round_number,
                                   cpu_s=None, peak_mib=None, wall_s=None, status=124,
                                   tests=None, failures=None, errors=None,
                                   instance_type=links['instance_type'], box='')
                        (work / 'output').touch()
                        if budget > 0:
                            before = time.monotonic()
                            with (work / 'output').open('wb') as output:
                                active = subprocess.Popen(['python3', 'scripts/test_times/module_run.py', 'scripts/' + folder, name],
                                    cwd=repo, env=environment, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
                                peak = 0.0
                                timed_out = False
                                try:
                                    while active.poll() is None:
                                        if not warming:
                                            peak = max(peak, process_memory_mib({active.pid}).get(active.pid, 0.0))
                                        remaining = budget - (time.monotonic() - before)
                                        if remaining <= 0:
                                            timed_out = True
                                            send_process_group(active.pid, signal.SIGKILL)
                                            break
                                        try:
                                            active.wait(timeout=min(0.02, remaining))
                                        except subprocess.TimeoutExpired:
                                            pass
                                    status = active.wait()
                                    row.update(status=124 if timed_out else status, peak_mib=peak)
                                finally:
                                    group = active.pid
                                    try:
                                        send_process_group(group, signal.SIGKILL)
                                    except ProcessLookupError:
                                        pass
                                    active.wait()
                                    while True:
                                        try:
                                            os.waitpid(-group, 0)
                                        except ChildProcessError:
                                            break
                                    active = None
                        timing = work / 'test-times.json'
                        if not warming and timing.exists():
                            measured = json.loads(timing.read_text())
                            for field, source in (('cpu_s', 'cpu_s'), ('wall_s', 'wall_s'), ('tests', 'tests_run'),
                                                  ('failures', 'failures'), ('errors', 'errors')):
                                value = measured[source]
                                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                                    raise ValueError('The timing output has an invalid ' + source)
                                if field in ('tests', 'failures', 'errors') and type(value) is not int:
                                    raise ValueError('The timing count must be an integer')
                                row[field] = value
                        if not warming:
                            rows.append(row)
                        with log.open('a') as output:
                            output.write((work / 'output').read_text(errors='replace') + '\n')
                            if not warming:
                                output.write(json.dumps(row) + '\n')
        stage('finish')
        reply['verdict'] = 'pass' if len(rows) == 2 * gate_request['rounds'] and all(row['status'] == 0 and row['tests'] is not None for row in rows) else 'fail'
    except BaseException:
        reply['error'] = traceback.format_exc()
        with log.open('a') as output:
            output.write(reply['error'])
    finally:
        if active is not None:
            send_process_group(active.pid, signal.SIGKILL)
            active.wait()
        reply['gate_s'] = time.monotonic() - started
        try:
            upload()
        except Exception as error:
            reply['log_error'] = str(error)
    return reply

result.clear()
result.update(paired_trial())
'''


BOX_CURL_RETRY = ('--retry', '2', '--retry-all-errors', '--retry-delay', '1')
GATE_RECORD_FILTER = r'''function remember(secret, i) {
    if (!length(secret) || seen[secret]++) return
    i = ++count
    while (i > 1 && length(secrets[i - 1]) < length(secret)) {
        secrets[i] = secrets[i - 1]
        i--
    }
    secrets[i] = secret
}
FILENAME == ARGV[1] {
    if (length($0)) {
        remember($0)
        query = substr($0, index($0, "?") + 1)
        fragment = index(query, "#")
        if (fragment) query = substr(query, 1, fragment - 1)
        remember(query)
    }
    next
}
{
    record = $0
    for (i = 1; i <= count; i++) {
        prefix = ""
        while ((offset = index(record, secrets[i])) > 0) {
            prefix = prefix substr(record, 1, offset - 1) "[URL removed]"
            record = substr(record, offset + length(secrets[i]))
        }
        record = prefix record
    }
    print record
}'''


def signed_urls(links: Mapping[str, object]) -> tuple[str, ...]:
    return tuple(link for link in links.values() if isinstance(link, str)
                 and link.startswith(('http://', 'https://')) and link.partition('?')[2].partition('#')[0])


class GuestTransferFailed(RuntimeError):
    pass


GUEST_TRANSFERS = f'''import json, os, subprocess, tempfile, time
from collections.abc import Mapping
{inspect.getsource(signed_urls)}
{inspect.getsource(GuestTransferFailed)}
gate_record_filter = {GATE_RECORD_FILTER!r}
# The controller validates the suite mode before it sends the request.
transfer_retries = {BOX_CURL_RETRY!r} if gate_request.get('task') != 'paired' and 'mode' in gate_request else ()
''' + r'''secret_file = root / 'transfer-secrets'
try:
    descriptor = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    pass
else:
    with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
        for secret in signed_urls(links):
            output.write(secret + '\n')
def safe_gate_record(content):
    return subprocess.run(['awk', gate_record_filter, str(secret_file), '-'],
                          input=content, stdout=subprocess.PIPE, check=True).stdout
def stage(name):
    try:
        with (root / 'boot.log').open('a') as output:
            output.write(time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()) + ' stage=' + name + '\n')
    except OSError:
        pass
def transfer(stage, name, arguments, *, content=None):
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=root, encoding='utf-8') as config:
            config.write('url = ' + json.dumps(links[name]) + '\n')
            config.flush()
            answer = subprocess.run(['curl', '--disable', '--config', config.name, '--fail', '--silent',
                                     '--write-out', '%{http_code}', *transfer_retries, *arguments],
                                    input=content, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        raise GuestTransferFailed(f'{stage} {name} failed (curl could not run)') from None
    if answer.returncode:
        status = answer.stdout
        http = status.decode() if len(status) == 3 and status.isdigit() else '000'
        raise GuestTransferFailed(f'{stage} {name} failed (curl exit {answer.returncode}, HTTP {http})')
def get(name, destination):
    transfer('GET', name, ['--max-time', '60', '-o', str(destination)])
def put(name, content):
    transfer('PUT', name, ['--max-time', '30', '-X', 'PUT', '--data-binary', '@-', '-o', '/dev/null'],
             content=safe_gate_record(content))
def publish(name):
    put(name, json.dumps(result).encode())
'''


CANDIDATE_GUEST = r'''import contextlib, json, os, pathlib, subprocess, threading, time, traceback
def busy_cpu_seconds():
    ticks = list(map(int, pathlib.Path('/proc/stat').read_text().splitlines()[0].split()[1:9]))
    return (sum(ticks) - ticks[3] - ticks[4] - ticks[7]) / os.sysconf('SC_CLK_TCK')
def sample_memory():
    try:
        fields = dict(line.split(':', 1) for line in pathlib.Path('/proc/meminfo').read_text().splitlines())
        used = (int(fields['MemTotal'].split()[0]) - int(fields['MemAvailable'].split()[0])) * 1024
        result['memory_peak_bytes'] = max(result['memory_peak_bytes'] or 0, used)
    except (OSError, ValueError, KeyError):
        pass
def monitor_memory(stop):
    while True:
        sample_memory()
        if stop.wait(2):
            return
memory_stop = threading.Event()
memory_thread = None
cpu_started = None
try:
    repo = root / 'repo'
    from scripts.production import gate as gate_runner
    from scripts.lib import cancellation
    timings = {}
    if 'module_seconds' in links:
        get('module_seconds', root / 'module_seconds.json')
        timings = json.loads((root / 'module_seconds.json').read_text())
    result['tools_ready_at'] = time.time()
    result['module_time_limit_s'] = links['module_time_limit_s']
    result['max_running_jobs'] = os.cpu_count() or 1
    result['vcpus'] = result['max_running_jobs']
    sample_memory()
    memory_thread = threading.Thread(target=monitor_memory, args=(memory_stop,), daemon=True)
    cancellation.start_masked_thread(memory_thread)
    stage('tests start')
    with log.open('w') as output, contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        cpu_started = busy_cpu_seconds()
        result['gate_started_at'] = time.time()
        if links.get('production'):
            gate = gate_runner.run_suites(repo, full=gate_request.get('full', False), machine=gate_runner.WholeMachine(timings),
                                        time_limit=(gate_runner.gate_record.GateTimeLimit(float(links['gate_time_limit_s']))
                                                    if float(links.get('gate_time_limit_s', 0)) > 0 else
                                                    gate_runner.gate_record.NoGateTimeLimit('no passing history on the controller')),
                                        module_time_limit_s=links['module_time_limit_s'],
                                        selected=gate_runner.SelectedModules(tuple(gate_request['modules'])),
                                        mode=gate_runner.SuiteMode(links['mode']))
        else:
            gate = gate_runner.run_suites(repo, full=True, machine=gate_runner.WholeMachine(timings),
                                        module_time_limit_s=links['module_time_limit_s'])
        if links.get('production'):
            result['record'] = completion_record(gate, links['attempt'], links['commit'], links['tree'], os.uname().nodename,
                                                 result['gate_started_at'], time.time(), links['mode'],
                                                 split=gate_request.get('split') is True)
            dirty = gate_request['dirty_paths']
            result['record'].update(head=gate_request['head'], dirty=bool(dirty), dirty_paths=dirty,
                                    dirty_path_count=len(dirty))
            result.update(attempt=links['attempt'], commit=links['commit'], tree=links['tree'],
                          host=os.uname().nodename, gate=encode_run(gate))
        result['gate_finished_at'] = time.time()
        cpu_finished = busy_cpu_seconds()
    result['verdict'] = 'pass' if gate.passed else 'fail'
    result['modules'] = []
    for job in gate.jobs:
        measured = job.times if isinstance(job.times, dict) else {}
        result['tests'] += measured.get('tests_run', 0)
        result['skips'] += measured.get('skipped', 0)
        name = job.suite + '/' + job.module
        result['modules'].append({'module': name, 'status': job.returncode, 'tests': measured.get('tests_run', 0),
                                  'skips': measured.get('skipped', 0), 'seconds': job.seconds, 'stopped': bool(job.killed_for)})
        if job.returncode:
            result['failed_modules'].append(name)
except BaseException:
    if 'gate_started_at' in result:
        result['gate_finished_at'] = time.time()
        cpu_finished = busy_cpu_seconds()
    result['verdict'] = 'error'
    result['error'] = traceback.format_exc()
    with log.open('a') as output:
        output.write(result['error'])
finally:
    if cpu_started is not None:
        if 'gate_finished_at' not in result:
            result['gate_finished_at'] = time.time()
            cpu_finished = busy_cpu_seconds()
        result['gate_s'] = result['gate_finished_at'] - result['gate_started_at']
        result['busy_cpu_s'] = cpu_finished - cpu_started
        result['cpu_use_percent'] = 100 * result['busy_cpu_s'] / max(0.001, result['vcpus'] * result['gate_s'])
    if memory_thread is not None:
        memory_stop.set()
        memory_thread.join()
        sample_memory()
'''


def run_guest(root: Path, links: dict[str, object], result: dict[str, object]) -> None:
    """Run the verified candidate with its own suite and record code."""
    request = (cast(dict[str, object], json.loads((root / 'request.json').read_text()))
               if links.get('production') else {})
    namespace: dict[str, object] = {
        'root': root, 'links': links, 'result': result, 'log': root / 'gate.log', 'gate_request': request,
        'encode_run': encode_run, 'completion_record': completion_record,
    }
    exec(GUEST_TRANSFERS, namespace)
    if request.get('task') == 'paired':
        from scripts.lib.descendants import adopt_descendants
        from scripts.lib.process_group import send_process_group
        namespace.update(
            timestamps={key: result[key] for key in ('nix_ready_at', 'user_data_started_at',
                       'kernel_boot_at', 'instance_running_at') if key in result},
            adopt_descendants=adopt_descendants, send_process_group=send_process_group)
        exec('import json, os, subprocess, sys, time, traceback\n' + PAIRED_GUEST, namespace)
        return
    exec(CANDIDATE_GUEST, namespace)


class GuestSourceRejected(ValueError):
    pass


def restore_private_source(root: Path, links: Mapping[str, str], request: dict[str, object],
                           get: Callable[[str, Path], None]) -> Path:
    """Check the bundles and recreate the exact requested trees without a remote."""
    repo = root / 'repo'
    repo.mkdir()

    def git(path: Path, *arguments: str) -> str:
        return subprocess.check_output(['git', '-C', str(path), *arguments], text=True).strip()

    def import_bundle(name: str, *refs: str) -> None:
        bundle = root / (name + '.bundle')
        get(name, bundle)
        with bundle.open('rb') as stream:
            if file_digest(stream, 'sha256').hexdigest() != links[name + '_sha256']:
                raise GuestSourceRejected(f'GuestSourceRejected: {name} checksum differs')
        _ = git(repo, 'bundle', 'verify', str(bundle))
        _ = git(repo, 'fetch', '--no-tags', str(bundle), *refs)

    def verify(path: Path, commit: str, tree: str) -> None:
        if (git(path, 'rev-parse', 'HEAD') != commit
                or git(path, 'rev-parse', 'HEAD^{tree}') != tree):
            raise GuestSourceRejected('GuestSourceRejected: the requested commit or tree differs')

    try:
        _ = git(repo, 'init', '-q')
        base = links['source_base_commit']
        import_bundle('source_base', base)
        if git(repo, 'rev-parse', 'FETCH_HEAD^{commit}') != base:
            raise GuestSourceRejected('GuestSourceRejected: the pinned base commit differs')
        _ = git(repo, 'update-ref', 'refs/gate-source/base', base)
        if 'source_delta' in links:
            import_bundle('source_delta', 'refs/cloud-gates/*:refs/gate-source/delta/*')
        origin_main = str(request['origin_main'])
        _ = git(repo, 'update-ref', 'refs/remotes/origin/main', origin_main)
        if request.get('task') == 'paired':
            for variant in ('base', 'changed'):
                revision = request[variant]
                if not isinstance(revision, dict):
                    raise GuestSourceRejected('GuestSourceRejected: the paired revision is invalid')
                identity = cast(dict[str, object], revision)
                checkout = root / variant
                commit = str(identity['commit'])
                checkout.mkdir()
                _ = git(checkout, 'init', '-q')
                # Share the verified objects for the life of the trial.
                alternates = checkout / '.git/objects/info/alternates'
                _ = alternates.write_text(str(repo.resolve() / '.git/objects') + '\n')
                _ = git(checkout, 'update-ref', 'refs/remotes/origin/main', origin_main)
                _ = git(checkout, 'checkout', '--detach', commit)
                verify(checkout, commit, str(identity['tree']))
            return root / 'changed'
        _ = git(repo, 'checkout', '--detach', links['commit'])
        verify(repo, links['commit'], links['tree'])
        return repo
    except GuestTransferFailed as error:
        raise GuestSourceRejected(f'GuestSourceRejected: {error}') from error
    except (OSError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        raise GuestSourceRejected('GuestSourceRejected: the requested source could not be restored') from error


PRIVATE_GUEST_SOURCE = (
    'from collections.abc import Callable, Mapping\nfrom hashlib import file_digest\nfrom pathlib import Path\n'
    + 'from typing import cast\n' + inspect.getsource(GuestSourceRejected) + '\n'
    + inspect.getsource(restore_private_source))


# The live bootstrap verifies the tree before it calls the candidate entry point.
GUEST = (r'''import concurrent.futures, datetime, importlib, json, os, pathlib, sqlite3, subprocess, sys, time, traceback
root = pathlib.Path(os.environ.pop('GATE_BOX_DIR', '/var/lib/gate-trial'))
os.environ.pop('box_dir', None)
links = json.loads((root / 'links.json').read_text())
gate_request = json.loads((root / 'request.json').read_text()) if links.get('production') else {}
timestamps = {'nix_ready_at': time.time()}
started_at = os.environ.pop('GATE_USER_DATA_STARTED_AT', '')
if started_at:
    timestamps['user_data_started_at'] = int(started_at)
try:
    timestamps['kernel_boot_at'] = next(int(line.split()[1]) for line in pathlib.Path('/proc/stat').read_text().splitlines()
                                        if line.startswith('btime '))
except (OSError, ValueError, StopIteration):
    pass
# Read cached metadata without a network request.
for metadata_path in ('/etc/ec2-metadata/instance-identity-document', '/run/cloud-init/instance-data.json'):
    try:
        metadata = json.loads(pathlib.Path(metadata_path).read_text())
        if 'ds' in metadata:
            metadata = metadata['ds']['dynamic']['instance-identity']['document']
        timestamps['instance_running_at'] = datetime.datetime.fromisoformat(metadata['pendingTime'].replace('Z', '+00:00')).timestamp()
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        continue
    break
''' + GUEST_TRANSFERS + PRIVATE_GUEST_SOURCE + r'''links['boot'] = 'warm' if (root / 'prepared').exists() else 'cold'
result = {**timestamps, 'verdict': 'ready', 'tests': 0, 'skips': 0, 'failed_modules': [], 'memory_peak_bytes': None}
log = root / 'gate.log'
try:
    if not (links.get('production') and gate_request.get('task') == 'paired'):
        result['tools'] = {}
        def tool_version(name):
            version = subprocess.run([name, '--version'], capture_output=True, text=True)
            return (version.stdout or version.stderr).strip()
        names = ('git', 'python3', 'cargo', 'rustc', 'rustdoc', 'rustup', 'cargo-nextest', 'uv', 'sqlite3')
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(names)) as tools:
            for name, version in zip(names, tools.map(tool_version, names)):
                result['tools'][name] = version
        stage('tools checked')
        result['tools']['python_sqlite'] = sqlite3.sqlite_version
    if links['step'] == 'start':
        result['tools_ready_at'] = time.time()
        links['boot'] = 'warm' if (root / 'prepared').exists() else 'cold'
    else:
        repo = root / 'repo'
        if gate_request.get('source_form') == 'regional-bundle':
            repo = restore_private_source(root, links, gate_request, get)
        elif links.get('production') and gate_request.get('task') == 'paired':
            subprocess.run(['git', 'clone', gate_request['clone_url'], str(repo)], check=True)
            for variant in ('base', 'changed'):
                revision = gate_request[variant]
                path = root / variant
                subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '--detach', str(path), revision['commit']], check=True)
                tree = subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD^{tree}'], text=True).strip()
                if tree != revision['tree']:
                    raise ValueError('The source tree differs from the request')
            repo = root / 'changed'
        else:
            if links.get('production'):
                subprocess.run(['git', 'clone', gate_request['clone_url'], str(repo)], check=True)
                if links.get('source'):
                    get('source', root / 'source.bundle')
                    subprocess.run(['git', '-C', str(repo), 'fetch', str(root / 'source.bundle'), links['commit']], check=True)
                subprocess.run(['git', '-C', str(repo), 'update-ref', 'refs/remotes/origin/main', gate_request['origin_main']], check=True)
            else:
                get('source', root / 'source.bundle')
                subprocess.run(['git', 'clone', str(root / 'source.bundle'), str(repo)], check=True)
            subprocess.run(['git', '-C', str(repo), 'checkout', '--detach', links['commit']], check=True)
            if links.get('production'):
                from subprocess import check_output
                tree = check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD^{tree}'], text=True).strip()
                if tree != links['tree']:
                    raise ValueError('The source tree differs from the request')
                subprocess.run(['git', '-C', str(repo), 'remote', 'set-url', 'origin', links['origin']], check=True)
        stage('source ready')
        home = pathlib.Path.home() / '.claude'
        home.mkdir(exist_ok=True)
        (home / 'config').symlink_to(repo / 'config', target_is_directory=True)
        os.chdir(repo)
        from types import ModuleType
        startup = ModuleType('_devtools_python_startup')
        startup.__file__ = __GATE_STARTUP_FILENAME__
        sys.modules[startup.__name__] = startup
        exec(__GATE_STARTUP_SOURCE__, startup.__dict__)
        startup.select_checkout(sys.argv)
        candidate_path = repo / 'scripts/aws_target/gate_box.py'
        if not candidate_path.is_file():
            raise RuntimeError('candidate predates guest entry point')
        candidate = importlib.import_module('scripts.aws_target.gate_box')
        if pathlib.Path(candidate.__file__).resolve() != candidate_path.resolve():
            raise RuntimeError('The guest entry point is outside the candidate tree')
        entry = getattr(candidate, 'run_guest', None)
        if not callable(entry):
            raise RuntimeError('candidate predates guest entry point')
        entry(root, links, result)
except Exception as error:
    result['verdict'] = 'error'
    rejected = isinstance(error, GuestSourceRejected) or str(error) == 'candidate predates guest entry point'
    result['error'] = str(error) if rejected else safe_gate_record(traceback.format_exc().encode()).decode()
    if rejected and 'boot_error' in links:
        put('boot_error', json.dumps(result).encode())
    with log.open('a') as output:
        output.write(result['error'])
finally:
    if not (links.get('production') and gate_request.get('task') == 'paired'):
        try:
            if links.get('production'):
                with log.open('a') as output:
                    output.write('Cloud gate: uploading\n')
            content = b''.join(path.read_bytes() for path in (root / 'boot.log', log, root / 'timeline.log') if path.exists())
            try:
                snapshot = subprocess.run(['ps', '-eo', 'etimes,pcpu,rss,args', '--sort=-etimes'], capture_output=True, text=True)
                rows = safe_gate_record(snapshot.stdout.encode()).decode().splitlines()
                lines = [line[:200] for line in rows if 'curl' not in line][:60]
                content += ('\n' + '\n'.join(lines) + '\n').encode()
            except (OSError, subprocess.SubprocessError):
                pass
            content += time.strftime('\nUTC: %Y-%m-%dT%H:%M:%SZ\n', time.gmtime()).encode()
            put('log', content)
        except Exception as error:
            result['log_error'] = str(error)
    stage('result upload')
    publish('result' if links['step'] == 'size' else 'ready-' + links['boot'])
    if links['step'] == 'start' and result['verdict'] == 'ready':
        (root / 'prepared').touch()
'''.replace('__GATE_STARTUP_SOURCE__', repr(inspect.getsource(python_startup)))
    .replace('__GATE_STARTUP_FILENAME__', repr(python_startup.__file__)))


SHELL_CURL = r'''gate_curl() {
    transfer_stage=$1
    shift
    curl_status=0
    http_code=$(curl --disable --fail --silent --write-out '%{http_code}' "$@" 2>/dev/null) || curl_status=$?
    if [ "$curl_status" -ne 0 ]; then
        case "$http_code" in
            [0-9][0-9][0-9]) ;;
            *) http_code=000 ;;
        esac
        printf '%s failed (curl exit %s, HTTP %s)\n' "$transfer_stage" "$curl_status" "$http_code" >&2
    fi
    return "$curl_status"
}
'''


def log_upload(*, retry: bool = True) -> str:
    retries = shlex.join(BOX_CURL_RETRY) + ' ' if retry else ''
    return f'''{{ cat "${{box_dir:-/var/lib/gate-trial}}/boot.log" "${{box_dir:-/var/lib/gate-trial}}/gate.log" "${{box_dir:-/var/lib/gate-trial}}/timeline.log" 2>/dev/null || :
    ps -eo etimes,pcpu,rss,args --sort=-etimes 2>/dev/null | safe_gate_record |
        awk '!/curl/ {{print substr($0,1,200); if (++n==60) exit}}' || :
    printf '\\n'
    date -u '+UTC: %Y-%m-%dT%H:%M:%SZ'
}} | safe_gate_record | gate_curl 'PUT log' --config "$box_dir/log.curl" {retries}--max-time 30 -X PUT --data-binary @- -o /dev/null || :'''


def log_push_loop(links: dict[str, str], interval_s: float) -> str:
    lock_start = ('if [ -e "$box_dir/measure" ]; then continue; fi\n        (\n        flock -x 9\n'
                  + '        if [ -e "$box_dir/measure" ]; then exit 0; fi\n'
                  if links.get('paired') else '')
    lock_end = ') 9> "$box_dir/upload.lock"' if links.get('paired') else ''
    return f'''push_logs() {{
    cpu_ticks() {{ awk '/^cpu / {{for (i=2;i<=9;i++) t+=$i; printf "%.0f %.0f\\n", t, $5+$6+$9; exit}}' /proc/stat; }}
    set -- $(cpu_ticks)
    previous_total=$1 previous_idle=$2
    while sleep {interval_s:g}; do
        {lock_start}        set -- $(cpu_ticks)
        busy=$(awk -v total="$(($1-previous_total))" -v idle="$(($2-previous_idle))" 'BEGIN {{printf "%.2f", (total>0 ? 100*(total-idle)/total : 0)}}')
        previous_total=$1 previous_idle=$2
        printf '%s loadavg=%s %s processes=%s cpu_busy_percent=%s\\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$(cat /proc/loadavg)" "$(awk '/^MemAvailable:/' /proc/meminfo)" "$(ps -e --no-headers | wc -l)" "$busy" >> "${{box_dir:-/var/lib/gate-trial}}/timeline.log" || :
        {log_upload()}
        {lock_end}
    done
}}
push_logs &
log_push_pid=$!
'''


BWRAP_UNAVAILABLE_STAGE = 'bwrap unavailable: bwrap or unprivileged user namespaces do not work for gate-trial'


def boot_trap(cold: str, warm: str, *, production: bool, private_tools: bool = False) -> str:
    """Publish a shell failure before the tool shell can publish a result."""
    destination = f'destination="$box_dir/{cold}.curl"'
    if cold != warm:
        destination += f'\n        if [ -e "$box_dir/prepared" ]; then destination="$box_dir/{warm}.curl"; fi'
    poweroff = '    shutdown -h now || :\n' if production else ''
    tool_failure = 'GateToolsUnavailable' if private_tools else 'Tool preparation failed'
    return fr'''boot_failed() {{
    status=$?
    set +e
    trap - EXIT
    if [ -n "${{log_push_pid:-}}" ]; then kill "$log_push_pid" 2>/dev/null || :; fi
    if [ "$status" -ne 0 ]; then
        {destination}
        {log_upload(retry=False)}
        stage=$(sed -n 's/^[0-9T:Z-]\{{20\}} stage=//p' "$box_dir/boot.log" | tail -n 1)
        if [ "$stage" = 'tool preparation' ]; then
            message='{tool_failure}'
        elif [ "$stage" = '{BWRAP_UNAVAILABLE_STAGE}' ]; then
            message="stage=$stage"
        else
            message="The box failed at stage ${{stage:-boot}}"
        fi
        message=$(printf '%s' "$message" | LC_ALL=C tr -d '[:cntrl:]' | sed 's/[\\"]/\\&/g')
        file="${{box_dir:-/var/lib/gate-trial}}/boot-error.json"
        printf '{{"verdict":"error","error":"%s (exit %s)"}}' "$message" "$status" > "$file"
        if [ "$?" -eq 0 ]; then
            safe_gate_record < "$file" |
                gate_curl 'PUT boot error' --config "$destination" {shlex.join(BOX_CURL_RETRY)} --max-time 30 -X PUT --data-binary @- -o /dev/null || :
        fi
    fi
{poweroff}    exit "$status"
}}
trap boot_failed EXIT
'''


GATE_IMAGE_OWNER_TAG = 'cloud-claude-gate-image'


def user_data(links: dict[str, str], revision: str, lifetime_minutes: int, log_push_s: float,
              module_time_limit_s: float, *, tool_paths: tuple[str, ...] = ()) -> str:
    """Run directly in amazon-init on each boot."""
    from scripts.lib import conf
    if tool_paths:
        from scripts.aws_target.gate_source import NIX_BINARY_CACHE
        tools = '"$@"'
        prepare_tools = f'''set -- {shlex.join(tool_paths)}
if ! nix --extra-experimental-features nix-command path-info --recursive "$@" >/dev/null 2>&1; then
    nix --extra-experimental-features nix-command copy --from {NIX_BINARY_CACHE} "$@"
fi
'''
    else:
        packages = conf.read_or_repository('cloud_gate').words('packages')
        tools = shlex.join(f'github:NixOS/nixpkgs/{revision}#{name}' for name in packages)
        prepare_tools = ''
    if links['step'] == 'size':
        cold = warm = 'boot_error' if 'boot_error' in links else 'result'
    else:
        cold, warm = 'ready-cold', 'ready-warm'
    shell_links = {'log', 'guest', 'request', cold, warm}
    signed = set(signed_urls(links))
    link_rows = '\n'.join(
        f'{json.dumps(key)}\t{int(key in shell_links)}\t{int(value in signed)}\t{json.dumps(value)}'
        for key, value in {**links, 'module_time_limit_s': module_time_limit_s}.items())
    def download(key: str, name: str) -> str:
        return (f"gate_curl 'GET {key}' --config \"$box_dir/{key}.curl\" {shlex.join(BOX_CURL_RETRY)} "
                f'--max-time 60 -o "$box_dir/{name}"\n')
    downloads = download('guest', 'guest.py')
    if 'request' in links:
        downloads += download('request', 'request.json')
    production = bool(links.get('production'))
    # The boot script's 077 protects its files; tests expect the normal umask.
    session = '''set +x +v
umask 022
''' + SHELL_CURL + '''printf '%s stage=tools ready\\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
''' + downloads + f'''ln -sf "$(command -v bash)" /bin/bash
ln -sf "$(command -v zsh)" /bin/zsh
mkdir -p "$box_dir/tool-profile"
cp -a /run/current-system/sw/bin/. "$box_dir/tool-profile/"
ln -sf "$(command -v python3)" "$box_dir/tool-profile/python3"
ln -sf "$(command -v chromium)" "$box_dir/tool-profile/google-chrome"
mount --bind "$box_dir/tool-profile" /run/current-system/sw/bin
chown -R gate-trial:users "$box_dir"
runuser -u gate-trial -- env HOME=/home/gate-trial XDG_RUNTIME_DIR=/run/user/$(id -u gate-trial) DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u gate-trial)/bus bash -c '
tool_path=$PATH
unset __NIXOS_SET_ENVIRONMENT_DONE __ETC_PROFILE_SOURCED
. /etc/profile
export PATH="$tool_path:$PATH"
if ! bwrap --ro-bind / / true; then
    printf "%s stage={BWRAP_UNAVAILABLE_STAGE}\\n" "$(date -u +%FT%TZ)" >&2
    exit 1
fi
printf "%s stage=bwrap ready\\n" "$(date -u +%FT%TZ)" >&2
exec python3 "$box_dir/guest.py"'
'''
    deadline = (f'systemd-run --unit=cloud-gate-expiry --on-calendar=@{int(links["expires"])} /run/current-system/sw/bin/shutdown -h now'
                if production else f'shutdown -h +{lifetime_minutes}')
    return f'''#!/bin/sh
set +x +v
GATE_USER_DATA_STARTED_AT=$(date -u +%s)
export GATE_USER_DATA_STARTED_AT
{deadline}
set -eu
umask 077
box_dir=${{GATE_BOX_DIR:-/var/lib/gate-trial}}
export box_dir GATE_BOX_DIR="$box_dir"
mkdir -p "$box_dir"
: > "$box_dir/boot.log"
exec >> "$box_dir/boot.log" 2>&1
export PATH=$PATH:/run/current-system/sw/bin
printf '{{' > "$box_dir/links.json"
: > "$box_dir/transfer-secrets"
tab=$(printf '\\t')
separator=
while IFS="$tab" read -r key shell_config signed_link value; do
    printf '%s\\n  %s: %s' "$separator" "$key" "$value" >> "$box_dir/links.json"
    separator=,
    if [ "$shell_config" -eq 1 ]; then
        name=${{key#\\"}}
        name=${{name%\\"}}
        printf 'url = %s\\n' "$value" > "$box_dir/$name.curl"
    fi
    if [ "$signed_link" -eq 1 ]; then
        url=${{value#\\"}}
        url=${{url%\\"}}
        printf '%s\\n' "$url" >> "$box_dir/transfer-secrets"
    fi
done <<'GATE_LINKS'
{link_rows}
GATE_LINKS
printf '\\n}}\\n' >> "$box_dir/links.json"
safe_gate_record() {{
    awk {shlex.quote(GATE_RECORD_FILTER)} "$box_dir/transfer-secrets" -
}}
{SHELL_CURL}{boot_trap(cold, warm, production=production, private_tools=bool(tool_paths))}printf '%s stage=boot\\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
{log_upload()}
{log_push_loop(links, log_push_s)}touch "$box_dir/timeline.log"
id -u gate-trial >/dev/null 2>&1 || useradd -m -g users gate-trial
loginctl enable-linger gate-trial
export PYTHONUNBUFFERED=1
printf '%s stage=tool preparation\\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
{prepare_tools}nix --extra-experimental-features 'nix-command flakes' shell {tools} --command bash -ec {shlex.quote(session)}
'''



def encode_run(run: object) -> dict[str, object]:
    """Return the complete gate result for object transfer."""
    from scripts.test_times import gate_record
    from dataclasses import asdict
    from typing import cast
    if not isinstance(run, GateRun):
        raise TypeError('expected a completed gate')

    def encode_job(job: GateJobResult) -> dict[str, object]:
        row: dict[str, object] = asdict(job)
        row['kind'] = job.kind.name
        del row['wall_clock']
        if isinstance(job.wall_clock, gate_record.WallClockSpan):
            row['started_at'] = job.wall_clock.started_at
            row['ended_at'] = job.wall_clock.ended_at
        times = row['times']
        if isinstance(times, dict) and 'tests_run' in times:
            # The record carries the test times and read lists; natedev reads only the counts here.
            row['times'] = {key: value for key, value in cast(dict[str, object], times).items()
                            if key not in ('config_reads', 'folder_scans', 'content_reads')} | {'tests': []}
        return row

    jobs = [encode_job(job) for job in run.jobs]
    # A missing executions list means the executions are the jobs: no module ran twice.
    executions = {} if run.executions == run.jobs else {'executions': [encode_job(job) for job in run.executions]}
    return {'jobs': jobs, **executions,
            'seconds': run.seconds, 'peak_memory_mib': run.peak_memory_mib,
            'tests_reused': run.tests_reused, 'selection_certified': run.selection_certified,
            'time_limit_report': run.time_limit_report}


def decode_run(value: object) -> GateRun:
    """Validate the transferred result before computing its verdict."""
    from scripts.test_times import gate_record
    from scripts.aws_target.ledger import mapping, rows, text
    import math
    from typing import cast

    def number(row: dict[str, object], key: str) -> float:
        value = row[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f'{key}: expected a finite nonnegative number')
        try:
            result = float(value)
        except OverflowError as error:
            raise ValueError(f'{key}: expected a finite nonnegative number') from error
        if not math.isfinite(result) or result < 0:
            raise ValueError(f'{key}: expected a finite nonnegative number')
        return result

    def job(row: dict[str, object]) -> GateJobResult:
        status = row['returncode']
        if type(status) is not int:
            raise ValueError('expected an integer status')
        times = mapping(row['times'])
        measured = (cast(gate_record.ModuleTimes, cast(object, times)) if 'tests_run' in times
                    else gate_record.NoModuleTimes(str(times.get('reason', 'timing does not apply'))))
        restart = row['restart']
        if restart not in ('', 'hold'):
            raise ValueError('unknown restart')
        wall_clock_fields = {key: number(row, key) for key in ('started_at', 'ended_at') if key in row}
        if len(wall_clock_fields) == 2:
            wall_clock: gate_record.WallClockSpan | gate_record.WallClockNotRecorded = gate_record.WallClockSpan(
                wall_clock_fields['started_at'], wall_clock_fields['ended_at'])
        elif wall_clock_fields:
            wall_clock = gate_record.WallClockNotRecorded('box result has only one module time')
        else:
            wall_clock = gate_record.WallClockNotRecorded('box result has no module times')
        return GateJobResult(text(row, 'suite'), text(row, 'module'), status, number(row, 'seconds'),
                             str(row['output']), str(row['killed_for']), row['tripwire_tripped'] is True,
                             GateJobKind[text(row, 'kind')], number(row, 'cpu_seconds'), measured,
                             number(row, 'peak_mib'), restart, wall_clock=wall_clock)

    row = mapping(value)
    # A newer box leaves out the executions list when it equals the jobs list.
    for key in ('tests_reused', 'selection_certified'):
        if type(row[key]) is not bool:
            raise ValueError(f'{key}: expected a boolean')
    return GateRun(tuple(job(item) for item in rows(row, 'jobs')), number(row, 'seconds'),
                   number(row, 'peak_memory_mib'), row['tests_reused'] is True,
                   row['selection_certified'] is True, tuple(job(item) for item in rows(row, 'executions' if 'executions' in row else 'jobs')),
                   str(row.get('time_limit_report', '')))


def execution_records(value: GateRun) -> list[gate_record.Execution]:
    """Keep every attempt's output for the gate record writer."""
    from scripts.test_times import gate_record
    return [gate_record.Execution(job.suite, job.module, job.seconds, job.cpu_seconds, job.peak_mib,
                                  job.returncode, job.restart, job.times, job.output,
                                  wall_clock=job.wall_clock)
            for job in value.executions or value.jobs]


def completion_selection(mode: str, *, split: bool) -> Literal["full", "affected", "subset"]:
    """Classify a record from the assignment."""
    if split:
        return "subset"
    return "affected" if SuiteMode.step(mode) == SuiteMode.TARGETED.value else "full"


def completion_record(value: object, attempt: str, commit: str, tree: str, host: str,
                      started: float, ended: float, mode: str, *, split: bool = False) -> dict[str, object]:
    """Return an explicit buildlog record with the remote host identity."""
    from scripts.test_times import gate_record
    from datetime import UTC
    from scripts.buildlog import record, store
    import os
    if not isinstance(value, GateRun):
        raise TypeError('expected a completed gate')
    modules = gate_record.module_runs(execution_records(value), gate_record.slow_test_s())
    suites = [{'suite': suite, 'wall_s': max((job.seconds for job in value.jobs if job.suite == suite), default=0),
               'status': int(any(job.returncode for job in value.jobs if job.suite == suite))}
              for suite in sorted({job.suite for job in value.jobs})]
    return {'kind': 'gate_run', 'v': record.RECORD_VERSION, 'id': 'gate-' + attempt, 'host': host,
            'started_at': datetime.fromtimestamp(started, UTC).isoformat(),
            'ended_at': datetime.fromtimestamp(ended, UTC).isoformat(), 'wall_s': value.seconds,
            'hold_wait_s': 0, 'selection': completion_selection(mode, split=split),
            'reason': 'time limit' if value.time_limit_report else 'cloud gate',
            'vcpus': os.cpu_count() or 1, 'status': int(not value.passed), 'max_jobs': os.cpu_count() or 1,
            'peak_mib': value.peak_memory_mib, 'cap_mib': 0, 'checkout': '/var/lib/gate-trial/repo',
            'branch': 'HEAD', 'sha': commit, 'tree': tree, 'source': store.CLOUD_GATE_SOURCE, 'step': SuiteMode.step(mode),
            'aborted': False, 'dirty': False, 'dirty_paths': [], 'dirty_path_count': 0,
            'suites': suites, 'modules': modules, 'slow_test_s': gate_record.slow_test_s(),
            'tests_run': sum(module['tests_run'] for module in modules)}
