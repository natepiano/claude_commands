import type { EngineInterface } from "claude-code";

export const POLL_MS = 500;
export const BATCH = 20;
export const VERSION = "0.1.0";
export const PATHS = { outbox: "outbox", claimed: "claimed", results: "results", heartbeat: "heartbeat.json", errors: "errors" } as const;

export type OutboxEntry = {
  id: string;
  session_id: string;
  socket: string;
  text: string;
  sha256: string;
  deadline_ms: number;
};
export type OutboxClaim = { id: string; claimed_ms: number; run: string };
export type OutboxResult = {
  id: string;
  outcome: "delivered" | "failed" | "expired" | "withdrawn" | "abandoned";
  detail: string;
  sha256: string;
  claimed_ms: number;
  finished_ms: number;
};
export type Heartbeat = { session_id: string; at_ms: number; version: string };
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
export function isOutboxEntry(value: unknown): value is OutboxEntry {
  return record(value) && isEntryId(value.id) && typeof value.session_id === "string" && value.session_id !== ""
    && typeof value.socket === "string" && typeof value.text === "string" && hash(value.sha256) && timestamp(value.deadline_ms);
}
export function isOutboxClaim(value: unknown): value is OutboxClaim {
  return record(value) && isEntryId(value.id) && timestamp(value.claimed_ms) && typeof value.run === "string" && value.run !== "";
}
export function isOutboxResult(value: unknown): value is OutboxResult {
  return record(value) && isEntryId(value.id) && typeof value.outcome === "string"
    && ["delivered", "failed", "expired", "withdrawn", "abandoned"].includes(value.outcome)
    && typeof value.detail === "string" && hash(value.sha256) && timestamp(value.claimed_ms) && timestamp(value.finished_ms);
}
export function isHeartbeat(value: unknown): value is Heartbeat {
  return record(value) && typeof value.session_id === "string" && value.session_id !== ""
    && timestamp(value.at_ms) && typeof value.version === "string" && value.version !== "";
}
