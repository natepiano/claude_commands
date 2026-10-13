import { describe, expect, mock, test } from "claude-code/testing";
import type { On, FsEntry, SessionReceiveInput, SessionSendInput, SessionStartInput } from "claude-code";
import type { DeliveryRouteFile } from "../hooks/protocol";

const directory = "/test-state/message/postmaster";
const socket = "uds:/test-sockets/recipient.sock";
const start: SessionStartInput = { cwd: "/test-home", surface: null, isInteractive: false };
const answer: SessionReceiveInput = { origin: { kind: "peer" }, text:
  `<cross-session-message from="${socket}" from-name="Recipient" from-mode="auto">\nAnswer\n</cross-session-message>` };

type RealTimers = {
  setTimeout(callback: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
};
function isRealTimers(value: unknown): value is RealTimers {
  return typeof value === "object" && value !== null
    && "setTimeout" in value && typeof value.setTimeout === "function"
    && "clearTimeout" in value && typeof value.clearTimeout === "function";
}
async function withinHeartbeat(promise: Promise<boolean>): Promise<void> {
  const timers: unknown = globalThis;
  if (!isRealTimers(timers)) throw new Error("Test runtime timers unavailable");
  let cancel = () => {};
  const timeout = new Promise<boolean>(resolve => {
    const handle = timers.setTimeout(() => { resolve(false); }, 1500);
    cancel = () => timers.clearTimeout(handle);
  });
  try { expect(await Promise.race([promise, timeout])).toBe(true); }
  finally { cancel(); }
}

function hookFiles(on: On) {
  const files = new Map<string, string>();
  const listed: string[] = [];
  const received: SessionReceiveInput[] = [];
  const started: SessionStartInput[] = [];
  const sent: SessionSendInput[] = [];
  const at_ms = Date.now();
  const saved: DeliveryRouteFile = { route: "open", socket, sender: "Original sender",
    reply_to_session_id: "original-session", session_id: "recipient", at_ms };
  files.set(`${directory}/delivered/${at_ms}-1.json`, JSON.stringify(saved));
  let signalHeartbeat: (value: boolean) => void = () => {};
  const heartbeat = new Promise<boolean>(resolve => { signalHeartbeat = resolve; });
  let signalListing: () => void = () => {};
  const listing = new Promise<void>(resolve => { signalListing = resolve; });
  let beforeList: (path: string) => Promise<void> = async () => {};
  let beforeWrite: (path: string) => Promise<void> = async () => {};
  mock.env(on, { HOME: "/test-home", XDG_STATE_HOME: "/test-state" });
  on("session.id", () => ({ value: "postmaster-session" }));
  on("session.send", (_$, event) => { sent.push(event); return { isDelivered: true }; });
  on("session.start", (_$, event) => { started.push(event); return { cwd: event.cwd }; });
  on("session.receive", (_$, event) => { received.push(event); return { text: event.text }; });
  on("fs.exists", (_$, event) => ({ value: event.path === `${directory}/delivered` || files.has(event.path)
    || [...files.keys()].some(path => path.startsWith(event.path + "/")) }));
  on("fs.list", async (_$, event) => {
    listed.push(event.path);
    if (event.path === `${directory}/delivered`) signalListing();
    await beforeList(event.path);
    const entries: FsEntry[] = [];
    for (const [path, text] of files) {
      if (!path.startsWith(event.path + "/")) continue;
      const name = path.slice(event.path.length + 1);
      if (!name.includes("/")) entries.push({ name, kind: "file", size: text.length, mtimeMs: at_ms, isLink: false });
    }
    return { value: entries };
  });
  on("fs.read", (_$, event) => {
    if (event.as !== "text") throw new Error("Test files support text reads only");
    const text = files.get(event.path);
    if (text === undefined) throw new Error(`Missing test file: ${event.path}`);
    return { value: text };
  });
  on("fs.write", async (_$, event) => {
    await beforeWrite(event.path);
    files.set(event.path, event.text);
    if (event.path === `${directory}/heartbeat.json`) signalHeartbeat(true);
    return { value: undefined };
  });
  return { files, listed, received, started, sent, heartbeat, listing,
    onBeforeList: (action: typeof beforeList) => { beforeList = action; },
    onBeforeWrite: (action: typeof beforeWrite) => { beforeWrite = action; },
    routeLists: () => listed.filter(path => path === `${directory}/delivered`).length,
  };
}

describe("postmaster hook startup", () => {
  test("receive before start restores reply routes consumes without next and starts the outbox", async ($, on) => {
    const fake = hookFiles(on);
    expect(await $.session.receive(answer)).toEqual({ consumed: "postmaster reply" });
    expect(fake.received).toEqual([]);
    expect(fake.sent.map(message => message.to)).toEqual(["original-session"]);
    expect([...fake.files.keys()].filter(path => path.startsWith(`${directory}/replies/`)).length).toBe(1);
    await withinHeartbeat(fake.heartbeat);
    expect(fake.files.has(`${directory}/heartbeat.json`)).toBe(true);
    expect(fake.routeLists()).toBe(1);
    expect(fake.started).toEqual([]);
  });

  test("two start events share one route restoration", async ($, on) => {
    const fake = hookFiles(on);
    expect(await $.session.start(start)).toEqual({ cwd: start.cwd });
    expect(await $.session.start(start)).toEqual({ cwd: start.cwd });
    expect(fake.routeLists()).toBe(1);
    expect(fake.started).toEqual([start, start]);
    await withinHeartbeat(fake.heartbeat);
  });

  test("overlapping start and receive await one service start", async ($, on) => {
    const fake = hookFiles(on);
    let release: () => void = () => {};
    const gate = new Promise<void>(resolve => { release = resolve; });
    fake.onBeforeList(async path => { if (path === `${directory}/delivered`) await gate; });
    const starting = $.session.start(start);
    await fake.listing;
    const receiving = $.session.receive(answer);
    release();
    expect(await starting).toEqual({ cwd: start.cwd });
    expect(await receiving).toEqual({ consumed: "postmaster reply" });
    expect(fake.routeLists()).toBe(1);
    expect(fake.started).toEqual([start]);
    expect(fake.received).toEqual([]);
    await withinHeartbeat(fake.heartbeat);
  });

  test("failed start records its error passes next and a receive retries startup", async ($, on) => {
    const fake = hookFiles(on);
    let reject = true;
    fake.onBeforeList(async path => {
      if (path === `${directory}/delivered` && reject) { reject = false; throw new Error("route listing failed"); }
    });
    expect(await $.session.start(start)).toEqual({ cwd: start.cwd });
    expect(fake.started).toEqual([start]);
    const errors = [...fake.files].filter(([path]) => path.startsWith(`${directory}/errors/`));
    expect(errors.length).toBe(1);
    expect(errors.map(([, text]) => text).join("\n")).toContain("fs.list");
    expect(await $.session.receive(answer)).toEqual({ consumed: "postmaster reply" });
    expect(fake.received).toEqual([]);
    expect(fake.routeLists()).toBe(2);
    await withinHeartbeat(fake.heartbeat);
  });

  test("a failed receive start passes the unchanged event and another receive retries", async ($, on) => {
    const fake = hookFiles(on);
    let reject = true;
    fake.onBeforeList(async path => {
      if (path === `${directory}/delivered` && reject) { reject = false; throw new Error("route listing failed"); }
    });
    expect(await $.session.receive(answer)).toEqual({ text: answer.text });
    expect(fake.received).toEqual([answer]);
    expect(await $.session.receive(answer)).toEqual({ consumed: "postmaster reply" });
    expect(fake.received).toEqual([answer]);
    expect(fake.routeLists()).toBe(2);
    await withinHeartbeat(fake.heartbeat);
  });

  test("unrelated arrivals reach next with their full event unchanged", async ($, on) => {
    const fake = hookFiles(on);
    const arrivals: SessionReceiveInput[] = [
      { ...answer, origin: { kind: "task-notification" } },
      { origin: { kind: "bridge" }, text: "the user prompt" },
      { ...answer, text: answer.text.replace(socket, "uds:/test-sockets/unknown.sock") },
      { origin: { kind: "peer" }, text: "Message from shutdown: stop at a safe point" },
    ];
    for (const event of arrivals) expect(await $.session.receive(event)).toEqual({ text: event.text });
    expect(fake.received).toEqual(arrivals);
    expect(fake.sent).toEqual([]);
    expect([...fake.files.keys()].some(path => path.startsWith(`${directory}/replies/`))).toBe(false);
    await withinHeartbeat(fake.heartbeat);
  });

  test("a receive handler error passes the message to next unchanged", async ($, on) => {
    const fake = hookFiles(on);
    fake.onBeforeWrite(async path => {
      if (path.startsWith(`${directory}/replies/`)) throw new Error("reply log write failed");
    });
    expect(await $.session.receive(answer)).toEqual({ text: answer.text });
    expect(fake.received).toEqual([answer]);
    await withinHeartbeat(fake.heartbeat);
  });
});
