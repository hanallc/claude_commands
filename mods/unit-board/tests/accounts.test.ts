import { describe, expect, mock, test } from 'claude-code/testing';
import type { Engine } from 'claude-code/testing';
import type { CommandRunInput, On, TurnCompleteInput } from 'claude-code';
import { span } from '../hooks/lines';
import type { BoardLine, BoardSpan } from '../hooks/lines';
import { cellWidth } from '../hooks/geometry';
import type { AccountsBoardColumn, AccountsBoardRow, StoredAccountsStatus } from '../types/accounts';
import { CWD, drawnText, element, elements, hostEngine, NOW, rowLines, styledText } from './kit';

const HOME = '/test-home';
const USAGE = `${HOME}/.local/state/agent-notes/usage.json`;
const GROUPS = `${HOME}/.local/state/accounts/groups.json`;
const CACHE = `${HOME}/.local/state/accounts/role_map_cache.json`;
const CONFIG = `${HOME}/.claude/config/accounts.json`;
const INPUTS = [USAGE, GROUPS, CACHE, CONFIG];
const ARGV = [`${HOME}/.claude/scripts/lib/py`, `${HOME}/.claude/scripts/accounts/accounts.py`,
  'status', '--json', '--stored'];
const command = (name = 'showrunner:accounts', args = ''): CommandRunInput => ({ command: name, args,
  origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 170 } });
const start = { cwd: CWD, surface: null, isInteractive: false } as const;
const turn: TurnCompleteInput = { answer: 'finished', reason: 'answer', durationMs: 100,
  isAborted: false, turnId: 'accounts-turn' };
const props = { title: 'Accounts', isFocused: true, bodyColumns: 170,
  scroll: { offset: 0, bodyRows: 60 }, view: {}, placement: 'dock' as const };
const s = span;

/** The columns `accounts.status_board()` writes. */
const COLUMNS: AccountsBoardColumn[] = [
  { key: 'dot', heading: '', align: 'left' }, { key: 'label', heading: 'account', align: 'left' },
  { key: 'left', heading: 'left', align: 'right' }, { key: 'roles', heading: 'role', align: 'left' },
  { key: 'busy', heading: 'busy', align: 'right' }, { key: 'idle', heading: 'idle', align: 'right' },
  { key: 'paused', heading: 'paused', align: 'right' }, { key: 'exited', heading: 'exited', align: 'right' },
  { key: 'total', heading: 'total', align: 'right' }, { key: 'resets', heading: 'resets', align: 'right' },
  { key: 'credits', heading: 'credits', align: 'right' }, { key: 'enabled', heading: 'enabled', align: 'left' },
];
/** The next local midnight after NOW in New York; no test reaches it unless it says so. */
const STALE_AT = '2026-10-17T04:00:00+00:00';
const COUNT_KEYS = ['busy', 'idle', 'paused', 'exited', 'total'] as const;
type Counts = readonly [number, number, number, number, number];
interface RowSpec {
  dot?: BoardSpan['tone'];
  label?: string;
  left?: BoardSpan;
  role?: string;
  counts?: Counts;
  credits?: string;
}
const fact = (name: string, value: string): BoardLine => [s(name, 'inactive'), s(` ${value}`)];
/** One board row in the shape `accounts.status_board()` writes. */
function row(spec: RowSpec): AccountsBoardRow {
  const counts = spec.counts;
  return {
    cells: {
      dot: spec.dot ? [s('●', spec.dot)] : [], label: spec.label ? [s(spec.label, 'default', 'bold')] : [],
      left: spec.left ? [spec.left] : [], roles: spec.role ? [s(spec.role)] : [],
      ...Object.fromEntries(COUNT_KEYS.map((key, index) => [key, counts ? [s(String(counts[index]))] : []])),
      resets: [], credits: spec.credits ? [s(spec.credits)] : [], enabled: [],
    },
    facts: [
      ...(spec.left ? [[{ ...spec.left, text: `${spec.left.text} left` }]] : []),
      ...(spec.role ? [fact('role', spec.role)] : []),
      ...(counts ? COUNT_KEYS.map((key, index) => fact(key, String(counts[index]))) : []),
      ...(spec.credits ? [fact('credits', spec.credits)] : []),
    ],
  };
}
function statusFixture({ counts = [1, 1, 0, 1, 3] as Counts, role = 'unit directors' } = {}): StoredAccountsStatus {
  return { board: { columns: COLUMNS, stale_at: STALE_AT, rows: [
    row({ dot: 'success', label: 'Low', left: s('9%', 'error'), role, counts, credits: '42' }),
    row({ role: 'showrunners', counts: [1, 1, 0, 0, 2] }),
    row({ dot: 'error', label: 'Watch', left: s('24%', 'warning'), role: 'workers', counts: [2, 2, 0, 0, 4], credits: 'unlimited' }),
    row({ dot: 'error', label: 'Unknown', left: s('—') }),
  ] }, problems: ['Stored reading unavailable for one account.'] };
}
// The board accounts.status_board() writes for scripts/accounts/test_status_board.py fixture().
const READABLE_ROWS: AccountsBoardRow[] = [
  { cells: { dot: [s("●", "success")], label: [s("aLpha", "default", "bold")], left: [s("60%")], roles: [s("showrunners ✓")], busy: [s("1")], idle: [s("0")], paused: [s("0")], exited: [s("1")], total: [s("2")], resets: [s("14:00")], credits: [s("unlimited")], enabled: [s("yes")] },
    facts: [[s("60% left")], [s("role", "inactive"), s(" showrunners ✓")], [s("busy", "inactive"), s(" 1")], [s("idle", "inactive"), s(" 0")], [s("paused", "inactive"), s(" 0")], [s("exited", "inactive"), s(" 1")], [s("total", "inactive"), s(" 2")], [s("resets", "inactive"), s(" 14:00")], [s("credits", "inactive"), s(" "), s("unlimited")], [s("enabled")]] },
  { cells: { dot: [], label: [], left: [], roles: [s("unit directors")], busy: [s("2")], idle: [s("1")], paused: [s("0")], exited: [s("0")], total: [s("3")], resets: [], credits: [], enabled: [] },
    facts: [[s("role", "inactive"), s(" unit directors")], [s("busy", "inactive"), s(" 2")], [s("idle", "inactive"), s(" 1")], [s("paused", "inactive"), s(" 0")], [s("exited", "inactive"), s(" 0")], [s("total", "inactive"), s(" 3")]] },
  { cells: { dot: [s("●", "error")], label: [s("beta", "default", "bold")], left: [s("20%", "warning")], roles: [s("workers")], busy: [s("7")], idle: [s("5")], paused: [s("0")], exited: [s("0")], total: [s("12")], resets: [s("Sat 14:00")], credits: [s("13,446")], enabled: [s("")] },
    facts: [[s("20% left", "warning")], [s("role", "inactive"), s(" workers")], [s("busy", "inactive"), s(" 7")], [s("idle", "inactive"), s(" 5")], [s("paused", "inactive"), s(" 0")], [s("exited", "inactive"), s(" 0")], [s("total", "inactive"), s(" 12")], [s("resets", "inactive"), s(" Sat 14:00")], [s("credits", "inactive"), s(" "), s("13,446")]] },
  { cells: { dot: [s("●", "success")], label: [s("Charlie", "default", "bold")], left: [s("8%", "error")], roles: [s("workers")], busy: [s("0")], idle: [s("1")], paused: [s("0")], exited: [s("0")], total: [s("1")], resets: [s("Sun 14:00")], credits: [s("")], enabled: [s("")] },
    facts: [[s("8% left", "error")], [s("role", "inactive"), s(" workers")], [s("busy", "inactive"), s(" 0")], [s("idle", "inactive"), s(" 1")], [s("paused", "inactive"), s(" 0")], [s("exited", "inactive"), s(" 0")], [s("total", "inactive"), s(" 1")], [s("resets", "inactive"), s(" Sun 14:00")]] },
  { cells: { dot: [s("●", "success")], label: [s("Delta", "default", "bold")], left: [s("25%")], roles: [s("spare")], busy: [s("")], idle: [s("")], paused: [s("")], exited: [s("")], total: [s("")], resets: [s("23 Oct")], credits: [s("")], enabled: [s("")] },
    facts: [[s("25% left")], [s("role", "inactive"), s(" spare")], [s("resets", "inactive"), s(" 23 Oct")]] },
  { cells: { dot: [s("●", "error")], label: [s("Echo", "default", "bold")], left: [s("—")], roles: [s("showrunners")], busy: [s("0")], idle: [s("0")], paused: [s("0")], exited: [s("0")], total: [s("0")], resets: [s("")], credits: [s("")], enabled: [s("")] },
    facts: [[s("— left")], [s("role", "inactive"), s(" showrunners")], [s("busy", "inactive"), s(" 0")], [s("idle", "inactive"), s(" 0")], [s("paused", "inactive"), s(" 0")], [s("exited", "inactive"), s(" 0")], [s("total", "inactive"), s(" 0")]] },
  { cells: { dot: [], label: [], left: [], roles: [s("unit directors")], busy: [s("0")], idle: [s("0")], paused: [s("0")], exited: [s("0")], total: [s("0")], resets: [], credits: [], enabled: [] },
    facts: [[s("role", "inactive"), s(" unit directors")], [s("busy", "inactive"), s(" 0")], [s("idle", "inactive"), s(" 0")], [s("paused", "inactive"), s(" 0")], [s("exited", "inactive"), s(" 0")], [s("total", "inactive"), s(" 0")]] },
  { cells: { dot: [s("●", "success")], label: [s("Foxtrot", "default", "bold")], left: [s("90%")], roles: [s("spare")], busy: [s("")], idle: [s("")], paused: [s("")], exited: [s("")], total: [s("")], resets: [s("Thu 14:00")], credits: [s("")], enabled: [s("yes")] },
    facts: [[s("90% left")], [s("role", "inactive"), s(" spare")], [s("resets", "inactive"), s(" Thu 14:00")], [s("enabled")]] },
  { cells: { dot: [s("●", "success")], label: [s("Gamma", "default", "bold")], left: [s("74%")], roles: [s("workers")], busy: [s("1")], idle: [s("2")], paused: [s("0")], exited: [s("1")], total: [s("4")], resets: [s("24 Oct")], credits: [s("")], enabled: [s("")] },
    facts: [[s("74% left")], [s("role", "inactive"), s(" workers")], [s("busy", "inactive"), s(" 1")], [s("idle", "inactive"), s(" 2")], [s("paused", "inactive"), s(" 0")], [s("exited", "inactive"), s(" 1")], [s("total", "inactive"), s(" 4")], [s("resets", "inactive"), s(" 24 Oct")]] },
  { cells: { dot: [], label: [s("total", "default", "bold")], left: [], roles: [], busy: [s("11")], idle: [s("9")], paused: [s("0")], exited: [s("2")], total: [s("22")], resets: [], credits: [], enabled: [] },
    facts: [[s("busy", "inactive"), s(" 11")], [s("idle", "inactive"), s(" 9")], [s("paused", "inactive"), s(" 0")], [s("exited", "inactive"), s(" 2")], [s("total", "inactive"), s(" 22")]] },
];
function readableFixture(): StoredAccountsStatus {
  return { board: { columns: COLUMNS, stale_at: STALE_AT, rows: structuredClone(READABLE_ROWS) }, problems: [
    'Stored weekly reading is unavailable for the spare account; keep its previous assignment until another reading arrives.',
    'The role cache has an old showrunner assignment that needs refreshing before the next production starts.',
  ] };
}
// These literal rows specify the full table, the split table, and the narrow facts.
const TABLE_DRAWN_LINES: Record<number, readonly string[]> = {
  "30": [
    "● aLpha",
    "  60% left",
    "  role showrunners ✓ · busy 1",
    "  idle 0 · paused 0 · exited 1",
    "  total 2 · resets 14:00",
    "  credits unlimited · enabled",
    "  role unit directors · busy 2",
    "  idle 1 · paused 0 · exited 0",
    "  total 3",
    "● beta",
    "  20% left · role workers",
    "  busy 7 · idle 5 · paused 0",
    "  exited 0 · total 12",
    "  resets Sat 14:00",
    "  credits 13,446",
    "● Charlie",
    "  8% left · role workers",
    "  busy 0 · idle 1 · paused 0",
    "  exited 0 · total 1",
    "  resets Sun 14:00",
    "● Delta",
    "  25% left · role spare",
    "  resets 23 Oct",
    "● Echo",
    "  — left · role showrunners",
    "  busy 0 · idle 0 · paused 0",
    "  exited 0 · total 0",
    "  role unit directors · busy 0",
    "  idle 0 · paused 0 · exited 0",
    "  total 0",
    "● Foxtrot",
    "  90% left · role spare",
    "  resets Thu 14:00 · enabled",
    "● Gamma",
    "  74% left · role workers",
    "  busy 1 · idle 2 · paused 0",
    "  exited 1 · total 4",
    "  resets 24 Oct",
    "total",
    "  busy 11 · idle 9 · paused 0",
    "  exited 2 · total 22",
    "",
    "Problems",
    "• Stored weekly reading is",
    "  unavailable for the spare",
    "  account; keep its previous",
    "  assignment until another",
    "  reading arrives.",
    "• The role cache has an old",
    "  showrunner assignment that",
    "  needs refreshing before the",
    "  next production starts."
  ],
  "60": [
    "● aLpha",
    "  60% left · role showrunners ✓ · busy 1 · idle 0 · paused 0",
    "  exited 1 · total 2 · resets 14:00 · credits unlimited",
    "  enabled",
    "  role unit directors · busy 2 · idle 1 · paused 0",
    "  exited 0 · total 3",
    "● beta",
    "  20% left · role workers · busy 7 · idle 5 · paused 0",
    "  exited 0 · total 12 · resets Sat 14:00 · credits 13,446",
    "● Charlie",
    "  8% left · role workers · busy 0 · idle 1 · paused 0",
    "  exited 0 · total 1 · resets Sun 14:00",
    "● Delta",
    "  25% left · role spare · resets 23 Oct",
    "● Echo",
    "  — left · role showrunners · busy 0 · idle 0 · paused 0",
    "  exited 0 · total 0",
    "  role unit directors · busy 0 · idle 0 · paused 0",
    "  exited 0 · total 0",
    "● Foxtrot",
    "  90% left · role spare · resets Thu 14:00 · enabled",
    "● Gamma",
    "  74% left · role workers · busy 1 · idle 2 · paused 0",
    "  exited 1 · total 4 · resets 24 Oct",
    "total",
    "  busy 11 · idle 9 · paused 0 · exited 2 · total 22",
    "",
    "Problems",
    "• Stored weekly reading is unavailable for the spare",
    "  account; keep its previous assignment until another",
    "  reading arrives.",
    "• The role cache has an old showrunner assignment that",
    "  needs refreshing before the next production starts."
  ],
  "100": [
    "   account  left  role            busy  idle  paused  exited  total     resets    credits  enabled",
    "●  aLpha     60%  showrunners ✓      1     0       0       1      2      14:00  unlimited  yes",
    "                  unit directors     2     1       0       0      3",
    "●  beta      20%  workers            7     5       0       0     12  Sat 14:00     13,446",
    "●  Charlie    8%  workers            0     1       0       0      1  Sun 14:00",
    "●  Delta     25%  spare                                                 23 Oct",
    "●  Echo        —  showrunners        0     0       0       0      0",
    "                  unit directors     0     0       0       0      0",
    "●  Foxtrot   90%  spare                                              Thu 14:00             yes",
    "●  Gamma     74%  workers            1     2       0       1      4     24 Oct",
    "   total                            11     9       0       2     22",
    "",
    "Problems",
    "• Stored weekly reading is unavailable for the spare account; keep its previous assignment until",
    "  another reading arrives.",
    "• The role cache has an old showrunner assignment that needs refreshing before the next production",
    "  starts."
  ],
  "140": [
    "   account  left  role            busy  idle  paused  exited  total     resets    credits  enabled",
    "●  aLpha     60%  showrunners ✓      1     0       0       1      2      14:00  unlimited  yes",
    "                  unit directors     2     1       0       0      3",
    "●  beta      20%  workers            7     5       0       0     12  Sat 14:00     13,446",
    "●  Charlie    8%  workers            0     1       0       0      1  Sun 14:00",
    "●  Delta     25%  spare                                                 23 Oct",
    "●  Echo        —  showrunners        0     0       0       0      0",
    "                  unit directors     0     0       0       0      0",
    "●  Foxtrot   90%  spare                                              Thu 14:00             yes",
    "●  Gamma     74%  workers            1     2       0       1      4     24 Oct",
    "   total                            11     9       0       2     22",
    "",
    "Problems",
    "• Stored weekly reading is unavailable for the spare account; keep its previous assignment until another reading arrives.",
    "• The role cache has an old showrunner assignment that needs refreshing before the next production starts."
  ]
};
const LABELS = ['aLpha', 'beta', 'Charlie', 'Delta', 'Echo', 'Foxtrot', 'Gamma'];

function textInColor(tree: unknown, color: string): string {
  return styledText(tree).filter(part => part.color === color).map(part => part.text).join('');
}
function accountLines(tree: unknown, label: string): string[] {
  const lines = rowLines(tree);
  const first = lines.findIndex(line => line.startsWith('● ') && (line.slice(1).trimStart() === label || line.slice(1).trimStart().startsWith(label + ' ')));
  if (first < 0) return [];
  const rest = lines.slice(first + 1);
  const next = rest.findIndex(line => line.startsWith('● ') || line === '' || line === 'Problems' || line === 'total' || line.startsWith('   total '));
  return [lines[first]!, ...rest.slice(0, next < 0 ? rest.length : next)];
}
function normalized(lines: readonly string[]): string {
  return lines.join(' ').replace(/\s+/g, ' ').trim();
}
function accountsHost(on: On, missing: readonly string[] = []) {
  const host = hostEngine(on);
  const clock = mock.clock(on, { now: NOW });
  on('env.get', (_$, event) => ({ value: event.name === 'HOME' ? HOME : undefined }));
  const directories = new Set([HOME, `${HOME}/.local`, `${HOME}/.local/state`,
    `${HOME}/.local/state/agent-notes`, `${HOME}/.local/state/accounts`, `${HOME}/.claude`,
    `${HOME}/.claude/config`]);
  const mtimes = new Map<string, number>([...directories, ...INPUTS].map(path => [path, NOW - 1000]));
  for (const path of missing) mtimes.delete(path);
  let data: unknown = statusFixture();
  let failure = '';
  let pending: Promise<void> = Promise.resolve();
  let release = () => {};
  let signal = () => {};
  const fs: string[] = [];
  const processes: string[][] = [];
  const escaped: string[] = [];
  const stateWrites: string[] = [];
  let accountStateKind = 'closed';
  on('state.set', { plugin: 'unit-board', key: 'accountsPanes' }, (_$, e, next) => {
    const state = e.value['accounts'];
    accountStateKind = state?.kind ?? 'closed';
    stateWrites.push(accountStateKind);
    return next(e);
  });
  const isAccounts = (argv: readonly string[]) => argv.includes(`${HOME}/.claude/scripts/accounts/accounts.py`);
  on('session.cwd', () => ({ value: CWD }));
  on('fs.stat', (_$, e) => {
    fs.push(e.path);
    const mtimeMs = mtimes.get(e.path);
    if (mtimeMs === undefined) throw new Error('Missing fixture input');
    return { value: { kind: directories.has(e.path) ? 'dir' : 'file', size: 0, mtimeMs, isLink: false } };
  });
  on('fs.exists', (_$, e) => { fs.push(e.path); return { value: mtimes.has(e.path) }; });
  on('fs.list', (_$, e) => {
    fs.push(e.path);
    return { value: [...mtimes.keys()].filter(path => path.startsWith(e.path + '/') &&
      !path.slice(e.path.length + 1).includes('/')).map(path => ({ name: path.slice(e.path.length + 1),
        kind: directories.has(path) ? 'dir' as const : 'file' as const, size: 0,
        mtimeMs: mtimes.get(path)!, isLink: false })) };
  });
  on('fs.read', (_$, e) => { fs.push(e.path); throw new Error('The accounts pane must consume process JSON only'); });
  on('fs.write', (_$, e) => { fs.push(e.path); throw new Error('Unexpected fixture write'); });
  on('process.run', async (_$, e) => {
    processes.push([...e.argv]);
    if (isAccounts(e.argv)) {
      signal();
      await pending;
      if (failure) {
        const stderr = failure;
        failure = '';
        return { value: { exitCode: 1, stdout: '', stderr, isStdoutTruncated: false, isStderrTruncated: false } };
      }
      return { value: { exitCode: 0, stdout: JSON.stringify(data), stderr: '',
        isStdoutTruncated: false, isStderrTruncated: false } };
    }
    throw new Error('Unexpected process from the accounts pane');
  });
  on('session.send', (_$, e) => { escaped.push(e.text); throw new Error('Unexpected message from accounts pane'); });
  return { ...host, clock, fs, mtimes, processes, escaped, stateWrites,
    accountStateKind: () => accountStateKind,
    accountRuns: () => processes.filter(isAccounts),
    otherRuns: () => processes.filter(argv => !isAccounts(argv)),
    /** The process boundary takes any JSON, so a test can send a document that is not valid. */
    replace: (next: unknown) => { data = next; },
    failNext: () => { failure = 'Stored status failed for fixture@example.invalid.'; },
    clear: () => { fs.length = 0; processes.length = 0; stateWrites.length = 0; },
    change: (path: string) => { mtimes.set(path, clock.now() + 1); },
    pause: () => {
      const started = new Promise<void>(resolve => { signal = resolve; });
      pending = new Promise<void>(resolve => { release = resolve; });
      return { started, finish: () => release() };
    },
  };
}
async function mount($: Engine, surface: 'terminal' | 'desktop' = 'terminal', columns = props.bodyColumns) {
  return $.ui.mount({ plugin: 'unit-board', requestId: 'accounts', component: 'Pane', surface,
    props: { ...props, bodyColumns: columns } });
}
function hasSensitivePath(paths: readonly string[]) {
  return paths.some(path => /(?:\.claude\.json|auth\.json|id_token)/.test(path));
}

describe('accounts board drawing', () => {
  for (const surface of ['terminal', 'desktop'] as const) {
    for (const columns of [140, 100, 60, 30]) {
      test(`${surface}: exact board form at ${columns} cells keeps every fact and problem`, async ($, on) => {
        const fake = accountsHost(on);
        fake.replace(readableFixture());
        await $.command.run(command());
        const tree = await (await mount($, surface, columns)).drawn();
        const lines = rowLines(tree);
        expect(lines).toEqual(TABLE_DRAWN_LINES[columns]!);
        expect(lines.filter(line => line.startsWith('● ')).map(line => line.slice(1).trim().split(' ')[0]))
          .toEqual(LABELS);
        for (const line of lines) {
          expect(cellWidth(line)).toBeLessThanOrEqual(columns);
          expect(line).not.toContain('…');
        }
        const spans = styledText(tree);
        for (const label of LABELS) {
          const labels = spans.filter(part => part.text.includes(label));
          expect(labels.length).toBeGreaterThan(0);
          expect(labels.every(part => part.bold && part.color === 'default')).toBe(true);
        }
        for (const heading of ['account', 'left', 'role', 'busy', 'idle', 'paused', 'exited', 'total', 'resets', 'credits', 'enabled']) {
          const headings = spans.filter(part => part.bold && part.text.trim() === heading);
          expect(headings.length).toBe((columns <= 60 ? 0 : 1) + (heading === 'total' ? 1 : 0));
        }
        for (const node of elements(tree)) {
          const attributes = element(node.props);
          expect(attributes.color === 'subtle').toBe(false);
          expect(attributes.dimColor === true).toBe(false);
          expect(attributes.wrap === 'truncate-end').toBe(false);
        }
        const problemHeading = lines.indexOf('Problems');
        expect(lines[problemHeading - 1]).toBe('');
        expect(spans.some(part => part.text === 'Problems' && part.bold)).toBe(true);
        const headingSpan = spans.findIndex(part => part.text === 'Problems');
        expect(spans.slice(headingSpan + 1).filter(part => part.text.trim()).every(part => part.color === 'warning')).toBe(true);
      });
    }
  }

  test('column starts and right edges align across full and two-row tables', async ($, on) => {
    const fake = accountsHost(on);
    fake.replace(readableFixture());
    await $.command.run(command());
    for (const columns of [140, 80]) {
      const ui = await mount($, 'terminal', columns);
      const lines = rowLines(await ui.drawn());
      const header = lines[0]!;
      expect(header.indexOf('account')).toBe(3);
      expect(header.indexOf('role')).toBe(18);
      expect(header.indexOf('left')).toBe(12);
      expect(header.indexOf('busy')).toBe(34);
      expect(header.indexOf('idle')).toBe(40);
      expect(header.indexOf('exited')).toBe(54);
      expect(header.indexOf('total')).toBe(62);
      const firstRows = lines.filter(line => line.startsWith('● '));
      for (const [i, line] of firstRows.entries()) {
        expect(line.indexOf(LABELS[i]!)).toBe(3);
        expect(line.slice(18, 32).trim()).toBe(['showrunners ✓', 'workers', 'workers', 'spare', 'showrunners', 'spare', 'workers'][i]);
        expect(line.slice(12, 16).trim()).toBe(['60%', '20%', '8%', '25%', '—', '90%', '74%'][i]);
        expect(line[15]).toBe(i === 4 ? '—' : '%');
        expect(line.slice(34, 38).trim()).toBe(['1', '7', '0', '', '0', '', '1'][i]);
        expect(line.slice(40, 44).trim()).toBe(['0', '5', '1', '', '0', '', '2'][i]);
        expect(line.slice(54, 60).trim()).toBe(['1', '0', '0', '', '0', '', '1'][i]);
        expect(line.slice(62, 67).trim()).toBe(['2', '12', '1', '', '0', '', '4'][i]);
      }
      const detail = columns === 140 ? firstRows : firstRows.map(line => lines[lines.indexOf(line) + 1]!);
      const resetStart = columns === 140 ? 69 : 2;
      const creditStart = columns === 140 ? 80 : 13;
      const enabledStart = columns === 140 ? 91 : 24;
      for (const [i, line] of detail.entries()) {
        expect(line.slice(resetStart, resetStart + 9).trim()).toBe(['14:00', 'Sat 14:00', 'Sun 14:00', '23 Oct', '', 'Thu 14:00', '24 Oct'][i]);
        expect(line.slice(creditStart, creditStart + 9).trim()).toBe(i === 0 ? 'unlimited' : i === 1 ? '13,446' : '');
        expect(line.slice(enabledStart, enabledStart + 7).trim()).toBe(i === 0 || i === 5 ? 'yes' : '');
      }
      await ui.unmount();
    }
  });

  test('wide labels use terminal cells and keep all their characters when wrapping', async ($, on) => {
    const fake = accountsHost(on);
    const label = '用户名'.repeat(8) + ' é😀';
    const data = statusFixture();
    data.board.rows[0]!.cells.label = [s(label, 'default', 'bold')];
    data.problems = [];
    fake.replace(data);
    await $.command.run(command());
    const lines = rowLines(await (await mount($, 'terminal', 40)).drawn());
    for (const line of lines) expect(cellWidth(line)).toBeLessThanOrEqual(40);
    expect(lines.join('').replace(/\s/g, '')).toContain(label.replace(/\s/g, ''));
    expect(lines.join('')).not.toContain('…');
  });

  for (const columns of [140, 30]) {
    test(`${columns} cells: each span keeps the tone the board gives it`, async ($, on) => {
      const fake = accountsHost(on);
      const data = readableFixture();
      data.problems = [];
      fake.replace(data);
      await $.command.run(command());
      const tree = await (await mount($, 'terminal', columns)).drawn();
      expect(textInColor(tree, 'error').replace(/\s/g, '')).toBe(columns === 140 ? '●8%●' : '●8%left●');
      expect(textInColor(tree, 'warning').replace(/\s/g, '')).toBe(columns === 140 ? '20%' : '20%left');
      expect(textInColor(tree, 'success').replace(/\s/g, '')).toBe('●●●●●');
      const ordinary = textInColor(tree, 'default');
      for (const value of ['60%', '25%', '90%', '74%', '—', 'showrunners', 'unit directors', 'workers', 'spare',
        'unlimited', '13,446', '14:00', '24 Oct']) {
        expect(ordinary).toContain(value);
      }
    });
  }

  test('a missing cell draws blank and a short row leaves no padding at its edge', async ($, on) => {
    const fake = accountsHost(on);
    fake.replace({ board: { columns: COLUMNS, stale_at: STALE_AT, rows: [
      { cells: { dot: [s('●', 'success')], label: [s('Short', 'default', 'bold')] }, facts: [] },
      { cells: { label: [s('Gap', 'default', 'bold')], credits: [s('7')] }, facts: [] },
    ] }, problems: [] });
    await $.command.run(command());
    const [header, short, gap] = rowLines(await (await mount($)).drawn());
    expect(short).toBe('●  Short');
    expect(gap!.trimEnd()).toBe(gap);
    expect(gap!.indexOf('Gap')).toBe(header!.indexOf('account'));
    expect(gap!.length - 1).toBe(header!.indexOf('credits') + 'credits'.length - 1);
  });

  for (const upcoming of [
    ['Upcoming moves:', 'showrunners 1: at 1% → claude3, else claude2', '', 'units 1: at 1% → claude4', '', 'workers 1: at 1% → codex2'],
    ['Upcoming moves: none'],
    ['Upcoming moves: unavailable from natedev; next: remote list failed: fixture host unreachable'],
  ]) {
    test(`upcoming moves appear under the table: ${upcoming[0]}`, async ($, on) => {
      const fake = accountsHost(on);
      const data = statusFixture();
      data.upcoming_moves = upcoming;
      fake.replace(data);
      await $.command.run(command());
      const lines = rowLines(await (await mount($)).drawn());
      const heading = lines.indexOf(upcoming[0]!);
      const last = lines.findIndex(line => line.startsWith('●  Unknown'));
      const problems = lines.indexOf('Problems');
      expect(heading).toBeGreaterThan(last);
      expect(problems).toBeGreaterThan(heading);
      expect(lines.slice(heading, heading + upcoming.length)).toEqual(upcoming);
      expect(fake.accountRuns()).toEqual([ARGV]);
    });
  }
});

describe('accounts board stale time', () => {
  test('the pane reads the board again at its stale time with no file change', async ($, on) => {
    const fake = accountsHost(on);
    const before = statusFixture();
    before.board.stale_at = new Date(NOW + 15_000).toISOString();
    before.board.rows[0]!.cells.resets = [s('23:59')];
    fake.replace(before);
    await $.command.run(command());
    const ui = await mount($);
    expect(normalized(accountLines(await ui.drawn(), 'Low'))).toContain('23:59');
    const after = statusFixture();
    after.board.rows[0]!.cells.resets = [s('Fri 23:59')];
    fake.replace(after);
    fake.clear();
    await fake.clock.advance(10_000);
    expect(fake.accountRuns()).toEqual([]);
    await fake.clock.advance(10_000);
    expect(fake.accountRuns()).toEqual([ARGV]);
    expect(normalized(accountLines(await ui.drawn(), 'Low'))).toContain('Fri 23:59');
    fake.clear();
    await fake.clock.advance(20_000);
    expect(fake.accountRuns()).toEqual([]);
  });
});

describe('accounts board validation', () => {
  const invalid: [string, unknown][] = [
    ['no board', { problems: [] }],
    ['the old account rows', { accounts: [], problems: [] }],
    ['no stale time', { board: { columns: COLUMNS, rows: [] }, problems: [] }],
    ['a stale time that is not a time', { board: { columns: COLUMNS, stale_at: 'midnight', rows: [] }, problems: [] }],
    ['a column without an alignment', { board: { columns: [{ key: 'label', heading: 'account' }], stale_at: STALE_AT, rows: [] }, problems: [] }],
    ['a cell that is not a line', { board: { columns: COLUMNS, stale_at: STALE_AT, rows: [{ cells: { label: 'Low' }, facts: [] }] }, problems: [] }],
    ['an unknown tone', { board: { columns: COLUMNS, stale_at: STALE_AT, rows: [{ cells: { label: [{ text: 'Low', tone: 'blue', kind: 'fact' }] }, facts: [] }] }, problems: [] }],
    ['an unknown kind', { board: { columns: COLUMNS, stale_at: STALE_AT, rows: [{ cells: {}, facts: [[{ text: 'Low', tone: 'default', kind: 'italic' }]] }] }, problems: [] }],
  ];
  for (const [name, document] of invalid) {
    test(`a status with ${name} fails without drawing a row`, async ($, on) => {
      const fake = accountsHost(on);
      fake.replace(document);
      await $.command.run(command());
      expect(fake.accountStateKind()).toBe('failed');
      const text = drawnText(await (await mount($)).drawn());
      expect(/could not.*read|invalid/i.test(text)).toBe(true);
      expect(text).not.toContain('Low');
    });
  }

  test('a pane saved before the board existed draws as loading', async ($, on) => {
    accountsHost(on);
    // An older pane state that the current types cannot spell.
    const saved = { accounts: { kind: 'accounts', data: { accounts: [], problems: [] } } } as never;
    on('state.get', { plugin: 'unit-board', key: 'accountsPanes' }, () => ({ value: { value: saved, version: 1 } }));
    expect(drawnText(await (await mount($)).drawn())).toBe('Loading accounts…');
  });
});

describe('accounts pane', () => {
  test('local session inputs refresh role counts on every tick without a usage change or extra discovery', async ($, on) => {
    const fake = accountsHost(on);
    const path = `${HOME}/.claude/sessions/unit-a.json`;
    const data = statusFixture();
    data.session_inputs = [path];
    fake.mtimes.set(path, NOW - 1000);
    fake.replace(data);
    await $.command.run(command());
    const ui = await mount($);
    expect(normalized(accountLines(await ui.drawn(), 'Low'))).toContain('unit directors 1 1 0 1 3');
    fake.clear();
    const changed = statusFixture({ counts: [1, 2, 0, 1, 4] });
    changed.session_inputs = [path];
    fake.replace(changed);
    fake.change(path);
    await fake.clock.advance(10_000);
    expect(fake.accountRuns()).toEqual([ARGV]);
    expect(normalized(accountLines(await ui.drawn(), 'Low'))).toContain('unit directors 1 2 0 1 4');
    expect(fake.otherRuns()).toEqual([]);
    fake.clear();
    await fake.clock.advance(10_000);
    expect(fake.accountRuns()).toEqual([ARGV]);
    expect(fake.escaped).toEqual([]);
    expect(hasSensitivePath(fake.fs)).toBe(false);
  });

  test('a killed owner changes counts on the next tick even when every watched mtime stays unchanged', async ($, on) => {
    const fake = accountsHost(on);
    const path = `${HOME}/.claude/sessions/killed-owner.json`;
    const data = statusFixture();
    data.session_inputs = [path];
    fake.mtimes.set(path, NOW - 1000);
    fake.replace(data);
    await $.command.run(command());
    const ui = await mount($);
    expect(normalized(accountLines(await ui.drawn(), 'Low'))).toContain('unit directors 1 1 0 1 3');
    const unchanged = [...fake.mtimes.entries()];
    const afterExit = statusFixture({ counts: [1, 1, 0, 0, 2] });
    afterExit.session_inputs = [path];
    fake.replace(afterExit);
    fake.clear();
    await fake.clock.advance(10_000);
    expect([...fake.mtimes.entries()]).toEqual(unchanged);
    expect(fake.accountRuns()).toEqual([ARGV]);
    expect(fake.otherRuns()).toEqual([]);
    expect(normalized(accountLines(await ui.drawn(), 'Low'))).toContain('unit directors 1 1 0 0 2');
    expect(fake.escaped).toEqual([]);
    expect(fake.forbidden).toEqual([]);
  });

  test('a failed liveness refresh retries on the next unchanged tick', async ($, on) => {
    const fake = accountsHost(on);
    const path = `${HOME}/.claude/sessions/owner.json`;
    const data = statusFixture();
    data.session_inputs = [path];
    fake.mtimes.set(path, NOW - 1000);
    fake.replace(data);
    await $.command.run(command());
    const ui = await mount($);
    expect(normalized(accountLines(await ui.drawn(), 'Low'))).toContain('unit directors 1 1 0 1 3');
    const unchanged = [...fake.mtimes.entries()];
    fake.clear();
    fake.failNext();
    await fake.clock.advance(10_000);
    expect(fake.accountRuns()).toEqual([ARGV]);
    expect(fake.accountStateKind()).toBe('failed');
    const recovered = statusFixture({ counts: [1, 1, 0, 0, 2] });
    recovered.session_inputs = [path];
    fake.replace(recovered);
    fake.clear();
    await fake.clock.advance(10_000);
    expect([...fake.mtimes.entries()]).toEqual(unchanged);
    expect(fake.accountRuns()).toEqual([ARGV]);
    expect(fake.accountStateKind()).toBe('accounts');
    expect(normalized(accountLines(await ui.drawn(), 'Low'))).toContain('unit directors 1 1 0 0 2');
    expect(fake.otherRuns()).toEqual([]);
    expect(fake.escaped).toEqual([]);
    expect(fake.forbidden).toEqual([]);
  });

  for (const surface of ['terminal', 'desktop'] as const) {
    test(`${surface}: stored status opens the board with its problems`, async ($, on) => {
      const fake = accountsHost(on);
      await $.session.start(start);
      const result = await $.command.run(command());
      expect(result.text === 'Accounts opened.').toBe(true);
      expect(result.text?.includes('@') ?? false).toBe(false);
      expect(fake.accountRuns()).toEqual([ARGV]);
      expect(fake.opened).toEqual(['accounts']);
      const text = drawnText(await (await mount($, surface)).drawn());
      for (const word of ['●', 'left', 'resets', 'credits', 'Problems', 'Low', 'Watch', 'Unknown', 'showrunners', 'workers',
        'Stored reading unavailable for one account.', 'unlimited', '42']) {
        expect(text).toContain(word);
      }
      expect(text.includes('@')).toBe(false);
      expect(hasSensitivePath(fake.fs)).toBe(false);
      expect(fake.escaped.length).toBe(0);
      expect(fake.forbidden).toEqual([]);
    });
  }

  test('closing accounts acknowledges closure without an address', async ($, on) => {
    const fake = accountsHost(on);
    await $.session.start(start);
    await $.command.run(command());
    const result = await $.command.run(command('showrunner:accounts', 'close'));
    expect(result.text === 'Accounts closed.').toBe(true);
    expect(result.text?.includes('@') ?? false).toBe(false);
    expect(fake.closed).toEqual(['accounts']);
  });

  test('sibling changes beside existing inputs do not reread stored accounts', async ($, on) => {
    const fake = accountsHost(on);
    await $.session.start(start);
    await $.command.run(command());
    fake.clear();
    fake.change(`${HOME}/.local/state/accounts`);
    fake.change(`${HOME}/.local/state/accounts/roles.jsonl`);
    await fake.clock.advance(60_000);
    expect(fake.accountRuns()).toEqual([]);
    expect(fake.stateWrites.every(kind => kind === 'accounts')).toBe(true);
    expect(fake.fs.includes(`${HOME}/.local/state/accounts`)).toBe(false);
  });

  for (const source of INPUTS) {
    test(`a changed ${source.split('/').at(-1)} refreshes once and unchanged ticks stay quiet`, async ($, on) => {
      const fake = accountsHost(on);
      await $.session.start(start);
      await $.command.run(command());
      const ui = await mount($);
      fake.replace(statusFixture({ role: 'reviewers' }));
      fake.change(source);
      fake.clear();
      await fake.clock.advance(10_000);
      expect(fake.accountRuns()).toEqual([ARGV]);
      expect(drawnText(await ui.drawn()).includes('reviewers')).toBe(true);
      fake.clear();
      await fake.clock.advance(20_000);
      await $.turn.complete(turn);
      expect(fake.processes).toEqual([]);
      expect(hasSensitivePath(fake.fs)).toBe(false);
    });
  }

  test('an initially missing input stays quiet, then creation and deletion each refresh once', async ($, on) => {
    const fake = accountsHost(on, [GROUPS]);
    await $.session.start(start);
    await $.command.run(command());
    fake.clear();
    await fake.clock.advance(20_000);
    expect(fake.processes).toEqual([]);
    fake.change(GROUPS);
    fake.change(`${HOME}/.local/state/accounts`);
    await fake.clock.advance(10_000);
    expect(fake.accountRuns()).toEqual([ARGV]);
    fake.clear();
    await fake.clock.advance(10_000);
    expect(fake.processes).toEqual([]);
    fake.mtimes.delete(GROUPS);
    fake.change(`${HOME}/.local/state/accounts`);
    await fake.clock.advance(10_000);
    expect(fake.accountRuns()).toEqual([ARGV]);
    fake.clear();
    await fake.clock.advance(20_000);
    expect(fake.processes).toEqual([]);
  });

  test('closed accounts perform no file or process work and turn completion never refreshes them', async ($, on) => {
    const fake = accountsHost(on);
    await $.session.start(start);
    await $.turn.complete(turn);
    await fake.clock.advance(60_000);
    expect(fake.fs).toEqual([]);
    expect(fake.processes).toEqual([]);
    await $.command.run(command());
    fake.change(USAGE);
    fake.clear();
    await $.turn.complete(turn);
    expect(fake.fs).toEqual([]);
    expect(fake.processes).toEqual([]);
    await $.command.run(command('showrunner:accounts', 'close'));
    fake.clear();
    await fake.clock.advance(60_000);
    await $.turn.complete(turn);
    expect(fake.fs).toEqual([]);
    expect(fake.processes).toEqual([]);
  });

  test('closing accounts keeps the accounts lifecycle', async ($, on) => {
    const fake = accountsHost(on);
    await $.session.start(start);
    await $.command.run(command());
    await $.command.run(command('showrunner:accounts', 'close'));
    fake.clear();
    fake.change(USAGE);
    await fake.clock.advance(10_000);
    expect(fake.accountRuns().length).toBe(0);
    expect(fake.fs.includes(USAGE)).toBe(false);
    fake.clear();
    await fake.clock.advance(60_000);
    expect(fake.fs).toEqual([]);
    expect(fake.processes).toEqual([]);
  });

  for (const initial of [true, false]) {
    test(`${initial ? 'initial load' : 'refresh'} failure shows its reason and recovers on a watched change`, async ($, on) => {
      const fake = accountsHost(on);
      await $.session.start(start);
      if (!initial) await $.command.run(command());
      fake.failNext();
      if (initial) await $.command.run(command());
      else { fake.change(USAGE); await fake.clock.advance(10_000); }
      const ui = await mount($);
      expect(fake.accountStateKind()).toBe('failed');
      const failed = drawnText(await ui.drawn());
      expect(/failed|could not.*read/i.test(failed)).toBe(true);
      expect(failed.includes('@')).toBe(false);
      fake.change(USAGE);
      await fake.clock.advance(10_000);
      const text = drawnText(await ui.drawn());
      expect(fake.accountStateKind()).toBe('accounts');
      expect(text.includes('left')).toBe(true);
      expect(text.includes('9%')).toBe(true);
      expect(fake.accountRuns().every(argv => JSON.stringify(argv) === JSON.stringify(ARGV))).toBe(true);
    });
  }

  test('session restart resumes the existing accounts pane without opening it twice', async ($, on) => {
    const fake = accountsHost(on);
    await $.session.start(start);
    await $.command.run(command());
    await $.session.start(start);
    fake.clear();
    fake.change(USAGE);
    await fake.clock.advance(10_000);
    expect(fake.accountRuns()).toEqual([ARGV]);
    expect(fake.opened).toEqual(['accounts']);
  });

  for (const refresh of [false, true]) {
    test(`closing during pending ${refresh ? 'refresh' : 'open'} leaves no state or background work`, async ($, on) => {
      const fake = accountsHost(on);
      await $.session.start(start);
      if (refresh) await $.command.run(command());
      const pending = fake.pause();
      let opening: Promise<unknown> = Promise.resolve();
      if (refresh) { fake.change(USAGE); opening = fake.clock.advance(10_000); }
      else opening = $.command.run(command());
      await pending.started;
      await $.command.run(command('showrunner:accounts', 'close'));
      fake.clear();
      pending.finish();
      await opening;
      await fake.clock.settle();
      expect(fake.accountStateKind()).toBe('closed');
      expect(fake.stateWrites.length).toBe(0);
      expect(fake.fs).toEqual([]);
      await fake.clock.advance(10_000);
      expect(fake.accountRuns()).toEqual([]);
      expect(fake.otherRuns()).toEqual([]);
    });
  }
});
