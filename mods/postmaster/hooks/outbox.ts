import { BATCH, PATHS, POLL_MS, VERSION, isEntryId, isOutboxClaim, isOutboxEntry } from "./protocol";
import type { Heartbeat, Outbox, OutboxClaim, OutboxEntry, OutboxResult, PostmasterCalls } from "./protocol";

type EntryRead = { status: "valid"; entry: OutboxEntry } | { status: "unreadable" };
type PendingResult = { path: string; result: OutboxResult };

let errorCount = 0;

function errorText(error: unknown): string {
  if (typeof error === "object" && error !== null && "message" in error && typeof error.message === "string") return error.message;
  return error instanceof Error ? error.message : String(error);
}

async function stateDirectory(calls: PostmasterCalls): Promise<string> {
  const xdg = await calls.env.get("XDG_STATE_HOME");
  const root = typeof xdg === "string" && xdg !== "" ? xdg : `${await calls.env.get("HOME")}/.local/state`;
  if (!root.startsWith("/")) throw new Error("No absolute home or XDG_STATE_HOME is available");
  return `${root.replace(/\/$/, "")}/message/postmaster`;
}

async function writeError(calls: PostmasterCalls, directory: string, error: unknown): Promise<void> {
  const at_ms = calls.now();
  const n = ++errorCount;
  await calls.fs.write(`${directory}/${PATHS.errors}/${at_ms}-${n}.json`, JSON.stringify({ at_ms, error: errorText(error) })).catch(() => {});
}

export async function reportOutboxError(calls: PostmasterCalls, error: unknown): Promise<void> {
  try { await writeError(calls, await stateDirectory(calls), error); }
  catch { /* Without a state directory there is no permitted error path. */ }
}

async function textHash(text: string): Promise<string> {
  const bytes = new TextEncoder().encode(text);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
}

export async function startOutbox(calls: PostmasterCalls): Promise<Outbox> {
  const directory = await stateDirectory(calls);
  const session_id = await calls.session.id();
  if (session_id === "") throw new Error("No session id is available");
  const run = `${calls.now()}-${crypto.randomUUID()}`;
  const pending = new Map<string, PendingResult>();
  let polling = false;
  let cancelled = false;

  const path = (folder: string, id: string) => `${directory}/${folder}/${id}.json`;
  const heartbeat = async (): Promise<void> => {
    const value: Heartbeat = { session_id, at_ms: calls.now(), version: VERSION };
    await calls.fs.write(`${directory}/${PATHS.heartbeat}`, JSON.stringify(value));
  };
  const readEntry = async (id: string): Promise<EntryRead> => {
    try {
      const value: unknown = JSON.parse(await calls.fs.read(path(PATHS.outbox, id)));
      return isOutboxEntry(value) && value.id === id ? { status: "valid", entry: value } : { status: "unreadable" };
    } catch { return { status: "unreadable" }; }
  };
  const flush = async (): Promise<void> => {
    for (const [id, item] of pending) {
      try {
        await calls.fs.write(item.path, JSON.stringify(item.result));
        pending.delete(id);
      } catch (error) { await writeError(calls, directory, error); }
    }
  };
  const result = async (id: string, claimed_ms: number, sha256: string, outcome: OutboxResult["outcome"], detail: string): Promise<void> => {
    pending.set(id, { path: path(PATHS.results, id), result: { id, outcome, detail, sha256, claimed_ms, finished_ms: calls.now() } });
    await flush();
  };
  const deliver = async (id: string): Promise<void> => {
    if (pending.has(id) || await calls.fs.exists(path(PATHS.results, id)) || await calls.fs.exists(path(PATHS.claimed, id))) return;
    const read = await readEntry(id);
    const claimed_ms = calls.now();
    const claim: OutboxClaim = { id, claimed_ms, run };
    let sha256 = read.status === "valid" ? read.entry.sha256 : "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
    let sendAttempted = false;
    await calls.fs.write(path(PATHS.claimed, id), JSON.stringify(claim));
    try {
      const present = await calls.fs.exists(path(PATHS.outbox, id));
      sha256 = await textHash(read.status === "valid" ? read.entry.text : "");
      if (!present) return await result(id, claimed_ms, sha256, "withdrawn", "sender withdrew entry");
      if (read.status === "unreadable") return await result(id, claimed_ms, sha256, "failed", "unreadable entry");
      const entry = read.entry;
      if (entry.deadline_ms < calls.now()) return await result(id, claimed_ms, sha256, "expired", "entry deadline passed");
      sendAttempted = true;
      const answer = await calls.session.send({ to: { sessionId: entry.session_id }, text: entry.text });
      await result(id, claimed_ms, sha256, answer.isDelivered ? "delivered" : "failed", answer.isDelivered ? "" : answer.reason);
    } catch (error) {
      await writeError(calls, directory, error);
      await result(id, claimed_ms, sha256, sendAttempted ? "abandoned" : "failed", errorText(error));
    }
  };
  const oldClaim = async (id: string): Promise<void> => {
    if (pending.has(id) || await calls.fs.exists(path(PATHS.results, id))) return;
    const value: unknown = JSON.parse(await calls.fs.read(path(PATHS.claimed, id)));
    if (!isOutboxClaim(value) || value.id !== id) throw new Error(`Unreadable claim: ${id}`);
    if (value.run === run) return;
    const read = await readEntry(id);
    const sha256 = await textHash(read.status === "valid" ? read.entry.text : "");
    await result(id, value.claimed_ms, sha256, "abandoned", "earlier worker left a claim without a result");
  };
  const listIds = async (folder: string): Promise<string[]> => {
    if (!await calls.fs.exists(`${directory}/${folder}`)) {
      try { return await listedIds(folder); } catch { return []; }
    }
    return listedIds(folder);
  };
  const listedIds = async (folder: string): Promise<string[]> => {
    const files = await calls.fs.list(`${directory}/${folder}`);
    return files.filter(file => file.kind === "file" && !file.isLink && file.name.endsWith(".json") && isEntryId(file.name.slice(0, -5)))
      .sort((a, b) => a.mtimeMs - b.mtimeMs).map(file => file.name.slice(0, -5));
  };
  const pollBody = async (): Promise<void> => {
    if (polling || cancelled) return;
    polling = true;
    try {
      await heartbeat();
      await flush();
      for (const id of await listIds(PATHS.claimed)) {
        await oldClaim(id).catch(error => writeError(calls, directory, error));
      }
      let handled = 0;
      for (const id of await listIds(PATHS.outbox)) {
        if (handled === BATCH) break;
        await (async () => {
          if (pending.has(id) || await calls.fs.exists(path(PATHS.claimed, id)) || await calls.fs.exists(path(PATHS.results, id))) return;
          handled++;
          await heartbeat();
          await deliver(id);
        })().catch(error => writeError(calls, directory, error));
      }
    } finally { polling = false; }
  };
  const poll = (): Promise<void> => pollBody().catch(error => writeError(calls, directory, error));
  const timer = calls.every(POLL_MS, () => { void poll(); });
  return { poll, cancel: () => { cancelled = true; timer.cancel(); } };
}
