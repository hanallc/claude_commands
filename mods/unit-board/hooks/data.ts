import type { ProcessRunResult } from 'claude-code';
import type { AccountsBoard, StoredAccountsStatus } from '../types/accounts';

export type RefreshPaths =
  | { kind: 'discovery'; sources: string[]; loadedAt: number }
  | { kind: 'liveness' }
  | { kind: 'until'; at: number };
export interface RefreshFiles {
  stat(path: string): Promise<{ mtimeMs: number }>;
  now(): Promise<number>;
}
/** Shared by panes; it owns neither a timer nor any pane's state. */
export async function refreshIfChanged<T>(
  files: RefreshFiles,
  paths: readonly RefreshPaths[],
  generator: () => Promise<T>,
  set: (fresh: T) => Promise<void>,
  isOpen: () => boolean,
): Promise<boolean> {
  let changed = false;
  for (const path of paths) {
    if (!isOpen()) return false;
    // Process exits can leave every registry file unchanged. Recheck counts on
    // the existing pane tick, without a separate timer or remote discovery.
    if (path.kind === 'liveness') { changed = true; continue; }
    // Text that names a day, such as a reset time, changes at a set time with no file change.
    if (path.kind === 'until') { if (await files.now() >= path.at) changed = true; continue; }
    const written = path.loadedAt;
    for (const source of path.sources) {
      if (!isOpen()) return false;
      try { if ((await files.stat(source)).mtimeMs > written) changed = true; }
      catch { changed = true; }
    }
  }
  if (!changed || !isOpen()) return false;
  const fresh = await generator();
  if (!isOpen()) return false;
  await set(fresh);
  return true;
}

export interface AccountsLoader {
  home(): Promise<string | undefined>;
  now(): Promise<number>;
  run(argv: readonly string[]): Promise<ProcessRunResult>;
  stat(path: string): Promise<{ mtimeMs: number }>;
}

const TONES: readonly string[] = ['default', 'success', 'error', 'warning', 'inactive'];
const validLine = (line: unknown): boolean => Array.isArray(line) && line.every(part => part && typeof part === 'object'
  && typeof part.text === 'string' && TONES.includes(part.tone) && (part.kind === 'fact' || part.kind === 'bold'));
/** The board must name each column and give each row a line for each cell; the view only draws it. */
export function validBoard(value: unknown): value is AccountsBoard {
  if (!value || typeof value !== 'object') return false;
  const board = value as AccountsBoard;
  return typeof board.stale_at === 'string' && !Number.isNaN(Date.parse(board.stale_at)) && Array.isArray(board.columns) && board.columns.every(column => column && typeof column.key === 'string'
    && typeof column.heading === 'string' && (column.align === 'left' || column.align === 'right'))
    && Array.isArray(board.rows) && board.rows.every(row => row && typeof row.cells === 'object' && row.cells !== null
      && Object.values(row.cells).every(validLine) && Array.isArray(row.facts) && row.facts.every(validLine));
}

/** Only stored-status inputs and their existing discovery directories are inspected. */
export async function loadAccounts(
  calls: AccountsLoader, isOpen: () => boolean, watch: (paths: RefreshPaths[]) => void,
): Promise<StoredAccountsStatus> {
  const ensureOpen = () => { if (!isOpen()) throw new Error('Pane closed.'); };
  ensureOpen();
  const home = await calls.home();
  if (!home) throw new Error('Home directory is unavailable.');
  const loadedAt = await calls.now();
  const sources = new Set<string>();
  let refreshLiveness = false;
  let staleAt: RefreshPaths[] = [];
  const publish = () => watch([{ kind: 'discovery', sources: [...sources], loadedAt },
    ...(refreshLiveness ? [{ kind: 'liveness' } as const] : []), ...staleAt]);
  const inputs = [`${home}/.local/state/agent-notes/usage.json`, `${home}/.local/state/accounts/groups.json`,
    `${home}/.claude/config/accounts.json`, `${home}/.local/state/accounts/role_map_cache.json`];
  const watchInputs = async (inputs: readonly string[]) => {
    for (const input of inputs) {
      let path = input;
      while (path) {
        ensureOpen();
        try {
          await calls.stat(path);
          sources.add(path);
          break;
        } catch { /* A missing input is discovered through its closest existing parent. */ }
        path = path.slice(0, path.lastIndexOf('/')) || (path === '/' ? '' : '/');
      }
      publish();
    }
  };
  await watchInputs(inputs);
  ensureOpen();
  const result = await calls.run([`${home}/.claude/scripts/lib/py`, `${home}/.claude/scripts/accounts/accounts.py`,
    'status', '--json', '--stored']);
  ensureOpen();
  // Failure details never leave the pane loader: the pane shows a fixed reason, never process output.
  if (result.exitCode !== 0) throw new Error('Stored accounts status could not be read.');
  try {
    const status = JSON.parse(result.stdout) as StoredAccountsStatus;
    if (!validBoard(status.board) || !Array.isArray(status.problems)) throw new Error();
    staleAt = [{ kind: 'until', at: Date.parse(status.board.stale_at) }];
    publish();
    if (status.upcoming_moves !== undefined && (!Array.isArray(status.upcoming_moves)
      || !status.upcoming_moves.every(line => typeof line === 'string'))) throw new Error();
    if (status.session_inputs) {
      if (!Array.isArray(status.session_inputs) || !status.session_inputs.every(path => typeof path === 'string')) throw new Error();
      refreshLiveness = status.session_inputs.length > 0;
      await watchInputs(status.session_inputs);
    }
    return status;
  } catch { throw new Error('Stored accounts status is invalid.'); }
}
