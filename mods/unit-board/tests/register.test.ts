import { describe, expect, mock, test } from "claude-code/testing";
import type { CommandRunInput, TurnCompleteInput } from "claude-code";
import { refreshIfChanged } from "../hooks/data";
import { CWD, hostEngine, NOW } from "./kit";

const command = (args = ""): CommandRunInput => ({ command: "showrunner:accounts", args,
  origin: { kind: "composer" }, presentation: { isFullscreen: false, columns: 170 } });
const start = { cwd: CWD, surface: null, isInteractive: false } as const;
const turn: TurnCompleteInput = { answer: "work finished", reason: "answer", durationMs: 100,
  isAborted: false, turnId: "test-turn" };

describe("shared pane lifecycle", () => {
  test("startup registers no commands", async ($, on) => {
    mock.env(on, { HOME: "/test-home" });
    const registered: string[] = [];
    on("session.start", (_$, event) => ({ cwd: event.cwd }));
    on("ui.panes", () => ({ value: [] }));
    on("command.register", (_$, event) => {
      registered.push(event.name);
      return { value: { command: event.name } };
    });

    await $.session.start(start);
    expect(registered).toEqual([]);
  });

  test("the retired accounts command does not open a pane", async ($, on) => {
    const host = hostEngine(on);
    const processes: string[][] = [];
    on("process.run", (_$, event) => {
      processes.push([...event.argv]);
      throw new Error("A closed pane must not run a process.");
    });
    on("command.run", () => ({ text: "Command not handled by the mod." }));
    expect(await $.command.run({ ...command(), command: "accounts-board" }))
      .toEqual({ text: "Command not handled by the mod." });
    expect(host.opened).toEqual([]);
    expect(processes).toEqual([]);
  });

  test("a closed pane never requests a timer at startup or turn completion", async ($, on) => {
    mock.env(on, { HOME: "/test-home" });
    hostEngine(on);
    const requests: number[] = [];
    on("clock.every", (_$, event) => {
      requests.push(event.ms);
      throw new Error("Closed pane requested a repeating timer");
    });
    on("clock.after", (_$, event) => {
      requests.push(event.ms);
      throw new Error("Closed pane requested a timer");
    });
    await $.session.start(start);
    await $.turn.complete(turn);
    expect(requests).toEqual([]);
  });

  test("closed startup and turn completion perform no file, process, or timer work", async ($, on) => {
    const host = hostEngine(on);
    const clock = mock.clock(on, { now: NOW });
    const calls: string[] = [];
    on("fs.read", () => { calls.push("read"); throw new Error("A closed pane must not read files."); });
    on("fs.stat", () => { calls.push("stat"); throw new Error("A closed pane must not inspect files."); });
    on("process.run", () => { calls.push("process"); throw new Error("A closed pane must not run a process."); });
    await $.session.start(start);
    await $.turn.complete({ answer: "finished", reason: "answer", durationMs: 100,
      isAborted: false, turnId: "closed-turn" });
    await clock.advance(60_000);
    expect(calls).toEqual([]);
    expect(host.opened).toEqual([]);
    expect(host.forbidden).toEqual([]);
  });

  test("the shared stat helper refreshes only the pane whose sources changed", async () => {
    const mtimes = new Map([["/a.json", 100], ["/a-source", 101],
      ["/b.json", 100], ["/b-source", 99]]);
    const services = { stat: async (path: string) => {
      const mtimeMs = mtimes.get(path);
      if (mtimeMs === undefined) throw new Error(`Missing ${path}`);
      return { mtimeMs };
    }, now: async () => 100 };
    const generated: string[] = [];
    const states: string[] = [];
    const refresh = async (pane: string) => refreshIfChanged(services,
      [{ kind: "discovery", loadedAt: 100, sources: [`/${pane}-source`] }],
      async () => { generated.push(pane); return `${pane} refreshed`; },
      async value => { states.push(value); }, () => true);
    expect(await refresh("a")).toBe(true);
    expect(await refresh("b")).toBe(false);
    expect(generated).toEqual(["a"]);
    expect(states).toEqual(["a refreshed"]);
    mtimes.delete("/b-source");
    expect(await refresh("b")).toBe(true);
    expect(generated).toEqual(["a", "b"]);
    expect(states).toEqual(["a refreshed", "b refreshed"]);
  });

});
