import type { On, UiPane } from "claude-code";

export const NOW = Date.UTC(2026, 9, 16, 18, 0);
export const CWD = "/test-worktree";
export const SESSION = "showrunner-session-id";
export const sessionFixture = { cwd: async () => CWD, id: async () => SESSION };
export function hostEngine(on: On, sessionId: () => string | Promise<string> = sessionFixture.id) {
  const panes = new Map<string, UiPane>();
  const opened: string[] = [];
  const closed: string[] = [];
  const forbidden: string[] = [];
  on("session.id", async () => ({ value: await sessionId() }));
  on("session.start", (_$, event) => ({ cwd: event.cwd }));
  on("turn.complete", (_$, event) => ({ text: event.answer }));
  on("command.register", (_$, event) => ({ value: { command: event.name } }));
  on("ui.panes", () => ({ value: [...panes.values()] }));
  on("ui.open", (_$, event) => {
    opened.push(event.id);
    panes.set(event.id, { id: event.id, title: event.title ?? event.id,
      isShown: true, isFocused: true, isPlaced: true });
    return { value: { isPlaced: true } };
  });
  on("ui.close", (_$, event) => {
    closed.push(event.id);
    panes.delete(event.id);
    return { value: undefined };
  });
  on("ui.status", () => { forbidden.push("status"); return { value: undefined }; });
  on("ui.toast", () => { forbidden.push("toast"); return { value: undefined }; });
  return { opened, closed, forbidden };
}
