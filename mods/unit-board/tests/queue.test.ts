import { expect, test } from 'claude-code/testing';
import type { EngineInterface, On } from 'claude-code';
import type { AccountsPaneState } from '../types';
import type { StoredAccountsStatus } from '../types/accounts';
import { register } from '../hooks/register';
import { NOW, sessionFixture } from './kit';

const HOME = '/test-home';
const command = () => ({ command: 'showrunner:accounts', args: '',
  origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 170 } });

function tablePaneLifetimeHost() {
  type Hook = (services: EngineInterface, event: unknown, next: (event: unknown) => Promise<unknown>) => unknown;
  type StoredTablePane = AccountsPaneState<StoredAccountsStatus>;
  const hooks = new Map<string, Hook>();
  const collect = (event: string, ...args: unknown[]) => {
    const filter = args.length > 1 ? args[0] as { command?: string } : {};
    hooks.set(filter.command ? `${event}:${filter.command}` : event, args.at(-1) as Hook);
    return { catch: () => {} };
  };
  register(collect as On, {});
  const states = new Map<string, Record<string, StoredTablePane>>();
  const panes = new Set<string>();
  const runs: string[][] = [];
  const timers: { active: boolean; tick: () => void }[] = [];
  let stateReadGate = { pending: Promise.resolve(), started: () => {}, armed: false };
  const services = {
    session: sessionFixture,
    env: { get: async () => HOME },
    command: { register: async () => ({}) },
    ui: {
      open: async ({ id }: { id: string }) => { panes.add(id); return {}; },
      panes: async () => [...panes].map(id => ({ id })),
    },
    fs: { stat: async () => ({ mtimeMs: NOW - 1 }) },
    clock: { now: async () => NOW, every: (_ms: number, tick: () => void) => {
      const timer = { active: true, tick };
      timers.push(timer);
      return { cancel: () => { timer.active = false; } };
    } },
    state: {
      get: async ({ key }: { key: string }) => {
        const value = states.get(key);
        if (stateReadGate.armed) {
          stateReadGate.armed = false;
          stateReadGate.started();
          await stateReadGate.pending;
        }
        return { value };
      },
      set: async ({ key }: { key: string }, value: Record<string, StoredTablePane>) => {
        states.set(key, value);
      },
    },
    process: { run: async (argv: string[]) => {
      runs.push(argv);
      return { exitCode: 0, stdout: JSON.stringify({ board: { columns: [], rows: [], stale_at: '2026-10-17T04:00:00+00:00' }, problems: [] }),
        stderr: '', isStdoutTruncated: false, isStderrTruncated: false };
    } },
  } as unknown as EngineInterface;
  const dispatch = async (event: string, input: unknown) => hooks.get(event)!(services, input, async value => value);
  return { states, panes, runs, timers, dispatch,
    pauseStateRead: () => {
      let finish = () => {};
      const started = new Promise<void>(resolve => { stateReadGate.started = resolve; });
      stateReadGate.pending = new Promise<void>(resolve => { finish = resolve; });
      stateReadGate.armed = true;
      return { started, finish };
    },

  };
}

for (const pane of [
  { id: 'accounts', command: 'showrunner:accounts', key: 'accountsPanes', ready: 'accounts' },
]) {
  test(`${pane.id}: an older close cannot erase state written by a reopened pane`, async () => {
    const host = tablePaneLifetimeHost();
    const open = () => host.dispatch(`command.run:${pane.command}`, { ...command(), command: pane.command });
    await open();
    const held = host.pauseStateRead();
    const closing = host.dispatch('ui.close', { id: pane.id, origin: { kind: 'person' } });
    await held.started;
    try {
      await open();
      expect(host.states.get(pane.key)?.[pane.id]?.kind).toBe(pane.ready);
    } finally { held.finish(); }
    await closing;
    expect(host.states.get(pane.key)?.[pane.id]?.kind).toBe(pane.ready);
    expect(host.timers.filter(timer => timer.active).length).toBe(1);
  });
}


test('the plugin registers no build queue command or pane', () => {
  const commands: string[] = [];
  const panes: string[] = [];
  register(((event: string, ...args: unknown[]) => {
    const filter = args.length > 1 ? args[0] as { command?: string; requestId?: string } : {};
    if (filter.command) commands.push(filter.command);
    if (filter.requestId) panes.push(filter.requestId);
    return { catch: () => {} };
  }) as On, {});
  expect(commands).toEqual(['showrunner:accounts']);
  expect(panes).toEqual(['accounts']);
});
