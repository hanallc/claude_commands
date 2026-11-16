import type { EngineInterface, Register, Timer } from 'claude-code';
import { QueueView } from './queue';
import type { QueuePaneState, QueueLines, QueueSelection } from '../types/queue';

const QUEUE = 'build-queue';
const QUEUE_PANES = { plugin: 'unit-board', key: 'queuePanes' } as const;
let queueColumns = 100;
type QueueWidthRefresh = { kind: 'idle' } | { kind: 'requested'; columns: number };
let queueWidthRefresh: QueueWidthRefresh = { kind: 'idle' };
type TablePaneState<Ready> = { kind: 'loading' } | { kind: 'failed'; reason: string } | Ready;
type ClosedTablePane = { kind: 'closed'; generation: number };
type PreparingTablePane = { kind: 'preparing'; generation: number };
type OpenTablePane<Context> = { kind: 'open'; generation: number; timer: Timer; busy: boolean;
  refreshRequested: boolean; context: Context };
type TablePaneLifecycle<Context> = ClosedTablePane | PreparingTablePane | OpenTablePane<Context>;
interface TablePaneSettings {
  failure: string;
}
interface TablePaneCalls<Ready, Context> {
  prepare(isOpen: () => boolean): Promise<Context>;
  load(context: Context, isOpen: () => boolean): Promise<Ready>;
  put(state: TablePaneState<Ready>, isOpen: () => boolean): Promise<void>;
  drop(isClosed: () => boolean): Promise<void>;
  open(): Promise<unknown>;
  every(callback: () => void): Timer;
}

/** One lifecycle for one-shot table readers; closing invalidates every pending await. */
function tablePane<Ready extends { kind: 'queue' }, Context>(settings: TablePaneSettings) {
  let lifecycle: TablePaneLifecycle<Context> = { kind: 'closed', generation: 0 };
  let latestGeneration = 0;
  const read = async (calls: TablePaneCalls<Ready, Context>, current: OpenTablePane<Context>) => {
    const isOpen = () => lifecycle === current;
    const fresh = await calls.load(current.context, isOpen);
    if (isOpen()) await calls.put(fresh, isOpen);
  };
  const refresh = async (calls: TablePaneCalls<Ready, Context>, force = false) => {
    const current = lifecycle;
    if (current.kind !== 'open') return;
    if (current.busy) {
      if (force) current.refreshRequested = true;
      return;
    }
    const isOpen = () => lifecycle === current;
    current.busy = true;
    try {
      await read(calls, current);
    } catch {
      await calls.put({ kind: 'failed', reason: settings.failure }, isOpen);
    } finally {
      current.busy = false;
      if (isOpen() && current.refreshRequested) {
        current.refreshRequested = false;
        await refresh(calls, true);
      }
    }
  };
  const start = async (calls: TablePaneCalls<Ready, Context>, open: boolean) => {
    const preparing: PreparingTablePane = { kind: 'preparing', generation: ++latestGeneration };
    if (lifecycle.kind === 'open') lifecycle.timer.cancel();
    lifecycle = preparing;
    const isCurrent = () => lifecycle === preparing && latestGeneration === preparing.generation;
    const context = await calls.prepare(isCurrent);
    if (!isCurrent()) return false;
    if (open) await calls.open();
    if (!isCurrent()) return false;
    const current: OpenTablePane<Context> = { kind: 'open', generation: preparing.generation,
      context, busy: false, refreshRequested: false,
      timer: calls.every(() => { void refresh(calls); }) };
    lifecycle = current;
    if (open) {
      current.busy = true;
      try { await calls.put({ kind: 'loading' }, () => lifecycle === current); }
      finally { current.busy = false; }
    }
    await refresh(calls, open);
    return lifecycle === current;
  };
  const stop = async (calls: TablePaneCalls<Ready, Context>) => {
    const closed: ClosedTablePane = { kind: 'closed', generation: ++latestGeneration };
    if (lifecycle.kind === 'open') lifecycle.timer.cancel();
    lifecycle = closed;
    await calls.drop(() => lifecycle === closed && latestGeneration === closed.generation);
  };
  return { start, stop, refresh, checkpoint: () => ++latestGeneration,
    resume: async (calls: TablePaneCalls<Ready, Context>, generation: number) => {
      if (generation === latestGeneration) await start(calls, false);
    } };
}

async function queueRun($: EngineInterface, args: string[], isOpen = () => true) {
  const home = await $.env.get('HOME');
  if (!home || !isOpen()) throw new Error('Build queue could not be read.');
  const argv = [`${home}/.claude/scripts/lib/py`,
    `${home}/.claude/scripts/build_queue/build_queue.py`, ...args];
  const result = args[0] === 'bump' || args[0] === 'clear' || args[0] === 'admit-head'
    ? await $.process.run(argv, { env: { CLAUDE_CODE_SESSION_ID: await $.session.id() } })
    : await $.process.run(argv);
  if (!isOpen()) throw new Error('Build queue could not be read.');
  if (args[0] === 'bump' && result.exitCode === 3 && !result.isStdoutTruncated) {
    const selectorError = result.stdout.trim();
    if (/^build queue: [^\r\n]+$/.test(selectorError)) return selectorError;
  }
  if (result.exitCode !== 0 || result.isStdoutTruncated) throw new Error('Build queue could not be read.');
  return result.stdout;
}

type QueueReady = Extract<QueuePaneState, { kind: 'queue' }>;
function validQueueLines(value: unknown): value is QueueLines {
  if (!value || typeof value !== 'object') return false;
  const data = value as QueueLines;
  const tones = ['default', 'success', 'error', 'warning', 'inactive', 'suggestion'];
  return Number.isInteger(data.columns) && data.columns > 0 && Number.isFinite(data.as_of)
    && Array.isArray(data.lines) && data.lines.every(line => Array.isArray(line) && line.every(
      part => part && typeof part.text === 'string'
        && (part.tone === undefined || tones.includes(part.tone))
        && (part.bold === undefined || typeof part.bold === 'boolean')
        && (part.background === undefined || tones.includes(part.background))
        && (part.until === undefined || Number.isFinite(part.until))));
}
function normalizeQueueState(value: unknown): QueuePaneState {
  const saved = value && typeof value === 'object' ? value as Record<string, unknown> : {};
  const raw = saved.selection && typeof saved.selection === 'object'
    ? saved.selection as Record<string, unknown> : {};
  const selection: QueueSelection = raw.kind === 'card' && typeof raw.selector === 'string'
    ? { kind: 'card', selector: raw.selector }
    : raw.kind === 'view' ? { kind: 'view' }
    : typeof saved.selector === 'string' ? { kind: 'card', selector: saved.selector } : { kind: 'view' };
  if (saved.kind === 'queue' && validQueueLines(saved.data)) return { kind: 'queue', data: saved.data, selection };
  if (saved.kind === 'failed') return { kind: 'failed', reason: 'Build queue could not be read.', selection };
  return { kind: 'loading', selection };
}
type QueueOpening = { kind: 'saved' } | QueueSelection;
function queuePaneCalls($: EngineInterface, opening: QueueOpening = { kind: 'saved' }): TablePaneCalls<QueueReady, void> {
  return {
    open: () => $.ui.open({ id: QUEUE, title: 'Build queue', closeOnEscape: true }),
    every: callback => $.clock.every(10_000, callback),
    prepare: async isOpen => {
      const panes = (await $.state.get(QUEUE_PANES)).value ?? {};
      if (!isOpen()) return;
      const state: QueuePaneState = opening.kind === 'saved' ? normalizeQueueState(panes[QUEUE])
        : { kind: 'loading', selection: opening };
      await $.state.set(QUEUE_PANES, { ...panes, [QUEUE]: state });
    },
    load: async (...[, isOpen]) => {
      const state = normalizeQueueState((await $.state.get(QUEUE_PANES)).value?.[QUEUE]);
      if (!isOpen()) throw new Error('Pane closed.');
      const args = ['--json', '--columns', String(queueColumns), '--you', await $.session.id()];
      if (!isOpen()) throw new Error('Pane closed.');
      const selection = state.selection;
      if (selection.kind === 'card') args.push(selection.selector);
      const data: unknown = JSON.parse(await queueRun($, args, isOpen));
      if (!validQueueLines(data)) throw new Error('Invalid queue lines.');
      return { kind: 'queue', data, selection };
    },
    put: async (state, isOpen) => {
      if (!isOpen()) return;
      const previous = (await $.state.get(QUEUE_PANES)).value ?? {};
      if (!isOpen()) return;
      const saved = normalizeQueueState(previous[QUEUE]);
      if (state.kind === 'loading' && saved.kind === 'loading') return;
      if (state.kind === 'failed' && saved.kind === 'queue') return;
      const selected: QueuePaneState = state.kind === 'queue' ? state : { ...state, selection: saved.selection };
      await $.state.set(QUEUE_PANES, { ...previous, [QUEUE]: selected });
    },
    drop: async isClosed => {
      if (!isClosed()) return;
      const panes = { ...(await $.state.get(QUEUE_PANES)).value };
      if (!isClosed()) return;
      delete panes[QUEUE];
      await $.state.set(QUEUE_PANES, panes);
    },
  };
}

export const register: Register = on => {
  type CountdownRedraw = { kind: 'idle' } | { kind: 'ticking'; timer: Timer };
  let countdownRedraw: CountdownRedraw = { kind: 'idle' };
  let countdownGeneration = 0;
  const stopCountdown = () => {
    countdownGeneration++;
    if (countdownRedraw.kind === 'ticking') countdownRedraw.timer.cancel();
    countdownRedraw = { kind: 'idle' };
  };
  const queue = tablePane<QueueReady, void>({ failure: 'Build queue could not be read.' });

  on('session.start', async ($, e, next) => {
    const queueGeneration = queue.checkpoint();
    const panes = await $.ui.panes();
    if (panes.some(pane => pane.id === QUEUE)) await queue.resume(queuePaneCalls($), queueGeneration);
    return next(e);
  });
  on('command.run', { command: 'build:queue' }, async ($, e) => {
    const wanted = e.args.trim();
    if (wanted === 'close') {
      stopCountdown();
      await $.ui.close({ id: QUEUE });
      return { text: 'Build queue closed.' };
    }
    if (/^bump(?:\s|$)/.test(wanted)) {
      const words = wanted.split(/\s+/);
      if (words.length !== 2) return { text: 'build queue: use /build:queue bump <position|session|folder>' };
      let line: string;
      try {
        const output = await queueRun($, ['bump', words[1]!]);
        line = output.trim();
        if (!line || /[\r\n]/.test(line)) throw new Error('Invalid bump result.');
      } catch { return { text: 'Build queue could not be read.' }; }
      try { await queue.refresh(queuePaneCalls($), true); }
      catch { /* A pane refresh failure cannot replace the settled bump result. */ }
      return { text: line };
    }
    queueWidthRefresh = { kind: 'idle' };
    await queue.start(queuePaneCalls($, wanted ? { kind: 'card', selector: wanted } : { kind: 'view' }), true);
    return { text: 'Build queue opened.' };
  });
  on('ui.close', async ($, e, next) => {
    if (e.id === QUEUE) {
      stopCountdown();
      await queue.stop(queuePaneCalls($));
    }
    return next(e);
  });
  on('ui.render', { component: 'Pane', requestId: QUEUE }, async ($, e) => {
    const generation = countdownGeneration;
    queueColumns = Math.max(1, Math.floor(e.props.bodyColumns));
    const state = normalizeQueueState((await $.state.get(QUEUE_PANES)).value?.[QUEUE]);
    const elements = $.ui.resolve(e);
    const { Text } = elements;
    if (state.kind !== 'queue') {
      stopCountdown();
      return state.kind === 'loading' ? <Text color="inactive">Loading build queue…</Text>
        : <Text color="error">{state.reason}</Text>;
    }
    const now = await $.clock.now() / 1000;
    if (state.data.lines.some(line => line.some(part => part.until !== undefined && part.until > now))) {
      if ((await $.ui.panes()).some(pane => pane.id === QUEUE)
        && generation === countdownGeneration && countdownRedraw.kind === 'idle') countdownRedraw = { kind: 'ticking',
        timer: $.clock.every(1000, () => $.ui.invalidate('ui.render')) };
    } else stopCountdown();
    if (queueColumns !== state.data.columns
      && (queueWidthRefresh.kind === 'idle' || queueWidthRefresh.columns !== queueColumns)) {
      queueWidthRefresh = { kind: 'requested', columns: queueColumns };
      void queue.refresh(queuePaneCalls($), true);
    } else if (queueColumns === state.data.columns) queueWidthRefresh = { kind: 'idle' };
    return QueueView(elements, queueColumns, state.data, now);
  });
};
