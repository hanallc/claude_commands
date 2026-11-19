import type { EngineInterface, SessionReceiveInput } from "claude-code";

export const POLL_MS = 500;
export const BATCH = 20;
export const VERSION = "0.2.0";
export const REPLY_WINDOW_MS = 30 * 60 * 1000;
export const PATHS = { outbox: "outbox", claimed: "claimed", results: "results", heartbeat: "heartbeat.json", errors: "errors", delivered: "delivered", replies: "replies", forwards: "forwards" } as const;

export type OutboxRecipient = {
  session_id: string;
  recipient_pid: number;
  recipient_start: string;
};
export type OutboxEntry = OutboxRecipient & {
  id: string;
  socket: string;
  text: string;
  sha256: string;
  deadline_ms: number;
  sender: string;
  reply_to_session_id: string;
};
export type OutboxClaim = { id: string; claimed_ms: number; run: string };
const FIELDLESS_OUTCOMES = ["failed", "withdrawn", "abandoned"] as const;
const OUTBOX_OUTCOMES = ["delivered", "expired", ...FIELDLESS_OUTCOMES] as const;

type OutboxResultFields = {
  id: string;
  detail: string;
  sha256: string;
  claimed_ms: number;
  finished_ms: number;
};
type ResultWithoutRecipient = {
  outcome: (typeof FIELDLESS_OUTCOMES)[number];
  session_id?: never;
  recipient_pid?: never;
  recipient_start?: never;
};
export type OutboxResult = OutboxResultFields & (
  (OutboxRecipient & { outcome: (typeof OUTBOX_OUTCOMES)[number] }) | ResultWithoutRecipient
);
export type Heartbeat = { session_id: string; at_ms: number; version: string; run: string };
export type PostmasterCalls = {
  env: Pick<EngineInterface["env"], "get">;
  fs: Pick<EngineInterface["fs"], "write" | "exists" | "list"> & {
    read(path: string): Promise<string>;
  };
  session: Pick<EngineInterface["session"], "id" | "send">;
  now(): number;
  every(ms: number, fn: () => void): { cancel(): void };
};
export type Outbox = { poll(): Promise<void>; cancel(): void };
export type ReplyDestination = { kind: "session"; sessionId: string } | { kind: "unroutable" };
export type DeliveryRoute = {
  key: string;
  socket: string;
  sender: string;
  destination: ReplyDestination;
  sessionId: string;
  atMs: number;
};
export type RouteToken = { key: string };
export type ReplyDisposition = { kind: "consumed"; reason: "postmaster reply" } | { kind: "pass" };
export type DeliveryRouteFile = {
  route: "open" | "withdrawn";
  socket: string;
  sender: string;
  reply_to_session_id: string;
  session_id: string;
  at_ms: number;
};
export type ReplyLog = {
  at_ms: number;
  socket: string;
  name: string;
  session_id: string;
  senders: string[];
  body: string;
  targets: string[];
};
export type ForwardLog = { forwarded: string[] };
export type Replies = {
  sending(entry: OutboxEntry, atMs: number): Promise<RouteToken>;
  notDelivered(token: RouteToken): Promise<void>;
  receive(e: SessionReceiveInput): Promise<ReplyDisposition>;
};

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function timestamp(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}
export function isEntryId(value: unknown): value is string {
  return typeof value === "string" && /^[a-f0-9]{32}$/i.test(value);
}
function hash(value: unknown): value is string {
  return typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
}
function hasOutboxRecipient(value: Record<string, unknown>): boolean {
  return typeof value.session_id === "string" && value.session_id !== ""
    && timestamp(value.recipient_pid) && value.recipient_pid > 0
    && typeof value.recipient_start === "string" && value.recipient_start !== "";
}
export function isOutboxEntry(value: unknown): value is OutboxEntry {
  return record(value) && isEntryId(value.id) && hasOutboxRecipient(value)
    && typeof value.socket === "string" && typeof value.text === "string" && hash(value.sha256) && timestamp(value.deadline_ms)
    && typeof value.sender === "string" && value.sender !== "" && typeof value.reply_to_session_id === "string";
}
export function isOutboxClaim(value: unknown): value is OutboxClaim {
  return record(value) && isEntryId(value.id) && timestamp(value.claimed_ms) && typeof value.run === "string" && value.run !== "";
}
export function isFieldlessOutcome(value: unknown): value is (typeof FIELDLESS_OUTCOMES)[number] {
  return FIELDLESS_OUTCOMES.some(outcome => outcome === value);
}
export function isOutboxResult(value: unknown): value is OutboxResult {
  return record(value) && isEntryId(value.id) && OUTBOX_OUTCOMES.some(outcome => outcome === value.outcome)
    && typeof value.detail === "string" && hash(value.sha256) && timestamp(value.claimed_ms) && timestamp(value.finished_ms)
    && (hasOutboxRecipient(value) || (
      isFieldlessOutcome(value.outcome)
      && !("session_id" in value) && !("recipient_pid" in value) && !("recipient_start" in value)
    ));
}
export function isHeartbeat(value: unknown): value is Heartbeat {
  return record(value) && typeof value.session_id === "string" && value.session_id !== ""
    && timestamp(value.at_ms) && typeof value.version === "string" && value.version !== ""
    && typeof value.run === "string" && value.run !== "";
}
export function isDeliveryRouteFile(value: unknown): value is DeliveryRouteFile {
  return record(value) && (value.route === "open" || value.route === "withdrawn")
    && typeof value.socket === "string" && value.socket.startsWith("uds:/")
    && typeof value.sender === "string" && value.sender !== ""
    && typeof value.reply_to_session_id === "string"
    && typeof value.session_id === "string" && value.session_id !== "" && timestamp(value.at_ms);
}
function sessionIds(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((id: unknown) => typeof id === "string" && id !== "");
}
export function isReplyLog(value: unknown): value is ReplyLog {
  return record(value) && timestamp(value.at_ms)
    && typeof value.socket === "string" && value.socket.startsWith("uds:/")
    && typeof value.name === "string" && value.name !== ""
    && typeof value.session_id === "string" && value.session_id !== ""
    && Array.isArray(value.senders) && value.senders.length > 0
    && value.senders.every((sender: unknown) => typeof sender === "string" && sender !== "") && typeof value.body === "string"
    && sessionIds(value.targets);
}
export function isForwardLog(value: unknown): value is ForwardLog {
  return record(value) && sessionIds(value.forwarded);
}
