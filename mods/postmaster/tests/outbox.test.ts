import { describe, expect, test } from "claude-code/testing";
import type { FsEntry, SessionSendArgs, SessionSendResult } from "claude-code";
import type { OutboxEntry, PostmasterCalls, Replies, RouteToken } from "../hooks/protocol";
import { startOutbox } from "../hooks/outbox";

const directory = "/test-state/message/postmaster";
const helloHash = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824";
const firstId = "00000000000000000000000000000001";
const secondId = "00000000000000000000000000000002";

function entry(id = firstId, deadline_ms = 100000): OutboxEntry {
  return { id, session_id: "recipient", socket: "uds:/test-sockets/recipient.sock", sender: "Original sender", reply_to_session_id: "original-session", text: "hello", sha256: helloHash, deadline_ms };
}

function path(folder: string, id: string): string {
  return `${directory}/${folder}/${id}.json`;
}

function record(files: Map<string, string>, filename: string): Record<string, unknown> {
  const text = files.get(filename);
  if (text === undefined) throw new Error(`Missing test record: ${filename}`);
  const value: unknown = JSON.parse(text);
  if (typeof value !== "object" || value === null || Array.isArray(value)) throw new Error("Expected a JSON object");
  return value as Record<string, unknown>;
}

function fakeCalls() {
  const files = new Map<string, string>();
  const fileTimes = new Map<string, number>();
  const sent: SessionSendArgs[] = [];
  const writes: { path: string; text: string; at_ms: number }[] = [];
  const reads: string[] = [];
  const timers: { ms: number; fn: () => void; cancelled: boolean }[] = [];
  const environment = new Map<string, string>([["HOME", "/test-home"], ["XDG_STATE_HOME", "/test-state"]]);
  let now = 1000;
  let sendAnswer: (message: SessionSendArgs) => Promise<SessionSendResult> = async () => ({ isDelivered: true });
  let beforeRead: (filename: string) => Promise<void> = async () => {};
  let beforeWrite: (filename: string) => Promise<void> = async () => {};
  let afterWrite: (filename: string) => Promise<void> = async () => {};
  let beforeExists: (filename: string) => Promise<void> = async () => {};
  let afterExists: (filename: string, present: boolean) => Promise<void> = async () => {};
  const registered: OutboxEntry[] = [];
  const withdrawn: RouteToken[] = [];
  const tokens: RouteToken[] = [];
  const routeEvents: string[] = [];
  let registerRoute: (entry: OutboxEntry, atMs: number) => Promise<RouteToken> = async (item, atMs) => {
    registered.push(item);
    const token = { key: `${atMs}-${tokens.length + 1}` };
    tokens.push(token);
    routeEvents.push("route");
    return token;
  };
  const replies: Replies = {
    sending: (item, atMs) => registerRoute(item, atMs),
    notDelivered: async token => { withdrawn.push(token); routeEvents.push("withdrawn"); },
    receive: async () => ({ kind: "pass" }),
  };
  const calls: PostmasterCalls = {
    env: { get: async name => environment.get(name) },
    fs: {
      read: async filename => {
        reads.push(filename);
        await beforeRead(filename);
        const text = files.get(filename);
        if (text === undefined) throw new Error(`No test file: ${filename}`);
        return text;
      },
      write: async (filename, text) => {
        await beforeWrite(filename);
        files.set(filename, text);
        fileTimes.set(filename, now);
        writes.push({ path: filename, text, at_ms: now });
        await afterWrite(filename);
      },
      exists: async filename => {
        await beforeExists(filename);
        const present = files.has(filename) || [...files.keys()].some(path => path.startsWith(`${filename}/`));
        await afterExists(filename, present);
        return present;
      },
      list: async (folder = directory) => {
        if (![...files.keys()].some(filename => filename.startsWith(`${folder}/`))) throw new Error(`No test directory: ${folder}`);
        const entries: FsEntry[] = [];
        for (const [filename, text] of files) {
          const prefix = `${folder}/`;
          if (!filename.startsWith(prefix)) continue;
          const name = filename.slice(prefix.length);
          if (name.includes("/")) continue;
          entries.push({ name, kind: "file", size: text.length, mtimeMs: fileTimes.get(filename) ?? 0, isLink: false });
        }
        return entries;
      },
    },
    session: {
      id: async () => "postmaster-session",
      send: async message => {
        routeEvents.push("send");
        sent.push(message);
        return sendAnswer(message);
      },
    },
    now: () => now,
    every: (ms, fn) => {
      const timer = { ms, fn, cancelled: false };
      timers.push(timer);
      return { cancel: () => { timer.cancelled = true; } };
    },
  };
  return {
    calls, files, sent, writes, reads, timers, environment, replies, registered, withdrawn, tokens, routeEvents,
    answerRegistration: (answer: typeof registerRoute) => { registerRoute = answer; },
    put: (item: OutboxEntry) => {
      const filename = path("outbox", item.id);
      files.set(filename, JSON.stringify(item));
      fileTimes.set(filename, now);
    },
    setFileTime: (filename: string, at_ms: number) => { fileTimes.set(filename, at_ms); },
    result: (id: string) => record(files, path("results", id)),
    moveNow: (ms: number) => { now += ms; },
    answerSend: (answer: typeof sendAnswer) => { sendAnswer = answer; },
    onBeforeRead: (action: typeof beforeRead) => { beforeRead = action; },
    onBeforeWrite: (action: typeof beforeWrite) => { beforeWrite = action; },
    onAfterWrite: (action: typeof afterWrite) => { afterWrite = action; },
    onBeforeExists: (action: typeof beforeExists) => { beforeExists = action; },
    onAfterExists: (action: typeof afterExists) => { afterExists = action; },
  };
}

describe("outbox delivery", () => {
  test("delivers an entry once with the hash of the unchanged text", async () => {
    const fake = fakeCalls();
    fake.put({ ...entry(), sha256: "0".repeat(64) });
    const outbox = await startOutbox(fake.calls, fake.replies);
    await outbox.poll();
    await outbox.poll();
    expect(fake.sent).toEqual([{ to: { sessionId: "recipient" }, text: "hello" }]);
    expect(fake.registered).toEqual([{ ...entry(), sha256: "0".repeat(64) }]);
    expect(fake.routeEvents).toEqual(["route", "send"]);
    const result = fake.result(firstId);
    expect(result.id).toBe(firstId);
    expect(result.outcome).toBe("delivered");
    expect(result.sha256).toBe(helloHash);
    expect(result.claimed_ms).toBe(1000);
    expect(result.finished_ms).toBe(1000);
    expect(fake.files.has(path("outbox", firstId))).toBe(true);
    expect(fake.files.has(path("claimed", firstId))).toBe(true);
    expect(fake.timers.length).toBe(1);
    expect(fake.timers[0]?.ms).toBe(500);
    outbox.cancel();
    expect(fake.timers[0]?.cancelled).toBe(true);
  });

  test("an uppercase entry id is delivered and retains its case in the result", async () => {
    const fake = fakeCalls();
    const id = "ABCDEF0123456789ABCDEF0123456789";
    fake.put(entry(id));
    const outbox = await startOutbox(fake.calls, fake.replies);
    await outbox.poll();
    expect(fake.sent).toEqual([{ to: { sessionId: "recipient" }, text: "hello" }]);
    expect(fake.result(id).id).toBe(id);
    expect(fake.result(id).outcome).toBe("delivered");
    outbox.cancel();
  });

  test("a presence check failure after claiming produces failed without a later send", async () => {
    const fake = fakeCalls();
    fake.put(entry());
    let rejectCheck = true;
    fake.onBeforeExists(async filename => {
      if (filename === path("outbox", firstId) && rejectCheck) {
        expect(fake.files.has(path("claimed", firstId))).toBe(true);
        rejectCheck = false;
        throw new Error("entry presence check failed");
      }
    });
    const outbox = await startOutbox(fake.calls, fake.replies);
    await outbox.poll();
    const result = fake.result(firstId);
    expect(result.outcome).toBe("failed");
    expect(result.detail).toBe("entry presence check failed");
    expect(fake.sent).toEqual([]);
    await outbox.poll();
    await outbox.poll();
    expect(fake.sent).toEqual([]);
    expect(fake.result(firstId)).toEqual(result);
    outbox.cancel();
  });

  test("withdrawal after the claim produces withdrawn without a send", async () => {
    const fake = fakeCalls();
    fake.put(entry());
    fake.onAfterWrite(async filename => {
      if (filename === path("claimed", firstId)) fake.files.delete(path("outbox", firstId));
    });
    const outbox = await startOutbox(fake.calls, fake.replies);
    await outbox.poll();
    expect(fake.sent).toEqual([]);
    expect(fake.result(firstId).outcome).toBe("withdrawn");
    expect(fake.registered).toEqual([]);
    outbox.cancel();
  });

  const orders = [
    ["t1", "t2", "s1", "s2"],
    ["t1", "s1", "t2", "s2"],
    ["t1", "s1", "s2", "t2"],
    ["s1", "s2", "t1", "t2"],
    ["s1", "t1", "s2", "t2"],
    ["s1", "t1", "t2", "s2"],
  ];
  for (const order of orders) {
    test(`claim and withdrawal order ${order.join(" ")} delivers exactly once`, async () => {
      const fake = fakeCalls();
      fake.put(entry());
      const observed: string[] = [];
      let cursor = 0;
      let senderSawClaim = false;
      let entryPresentAtCheck = false;
      function senderSteps(): void {
        while (true) {
          const operation = order[cursor];
          if (operation !== "s1" && operation !== "s2") break;
          if (operation === "s1") fake.files.delete(path("outbox", firstId));
          else senderSawClaim = fake.files.has(path("claimed", firstId));
          observed.push(operation);
          cursor++;
        }
      }
      fake.onBeforeWrite(async filename => {
        if (filename === path("claimed", firstId)) senderSteps();
      });
      fake.onAfterWrite(async filename => {
        if (filename !== path("claimed", firstId)) return;
        expect(order[cursor]).toBe("t1");
        observed.push("t1");
        cursor++;
        senderSteps();
      });
      fake.onAfterExists(async (filename, present) => {
        if (filename !== path("outbox", firstId) || !observed.includes("t1")) return;
        expect(order[cursor]).toBe("t2");
        entryPresentAtCheck = present;
        observed.push("t2");
        cursor++;
        senderSteps();
      });
      const outbox = await startOutbox(fake.calls, fake.replies);
      await outbox.poll();
      expect(observed).toEqual(order);
      expect(fake.sent.length).toBe(entryPresentAtCheck ? 1 : 0);
      expect(fake.result(firstId).outcome).toBe(entryPresentAtCheck ? "delivered" : "withdrawn");
      const senderRelays = !senderSawClaim || fake.result(firstId).outcome === "withdrawn";
      expect(fake.sent.length + Number(senderRelays)).toBe(1);
      outbox.cancel();
    });
  }

  test("an expired entry is not sent", async () => {
    const fake = fakeCalls();
    fake.put(entry(firstId, 999));
    const outbox = await startOutbox(fake.calls, fake.replies);
    await outbox.poll();
    expect(fake.sent).toEqual([]);
    expect(fake.result(firstId).outcome).toBe("expired");
    expect(fake.registered).toEqual([]);
    outbox.cancel();
  });

  test("a returned refusal produces failed with its reason", async () => {
    const fake = fakeCalls();
    fake.put(entry());
    fake.answerSend(async () => ({ isDelivered: false, reason: "recipient rate limit" }));
    const outbox = await startOutbox(fake.calls, fake.replies);
    await outbox.poll();
    expect(fake.sent.length).toBe(1);
    expect(fake.result(firstId).outcome).toBe("failed");
    expect(fake.result(firstId).detail).toBe("recipient rate limit");
    expect(fake.routeEvents).toEqual(["route", "send", "withdrawn"]);
    expect(fake.withdrawn.length).toBe(1);
    expect(fake.withdrawn[0]).toBe(fake.tokens[0]);
    outbox.cancel();
  });

  test("route registration failure leaves the entry unsent with a failed result", async () => {
    const fake = fakeCalls();
    fake.put(entry());
    fake.answerRegistration(async () => { throw new Error("route write failed"); });
    const outbox = await startOutbox(fake.calls, fake.replies);
    try {
      await outbox.poll();
      await outbox.poll();
      expect(fake.sent).toEqual([]);
      expect(fake.withdrawn).toEqual([]);
      expect(fake.result(firstId).outcome).toBe("failed");
      expect(fake.result(firstId).detail).toBe("route write failed");
    } finally { outbox.cancel(); }
  });

  test("delivery followed by a throw produces abandoned and is not sent again", async () => {
    const fake = fakeCalls();
    fake.put(entry());
    const delivered: string[] = [];
    fake.answerSend(async message => {
      delivered.push(message.text);
      throw new Error("lost delivery answer");
    });
    const outbox = await startOutbox(fake.calls, fake.replies);
    await outbox.poll();
    await outbox.poll();
    expect(delivered).toEqual(["hello"]);
    expect(fake.sent.length).toBe(1);
    expect(fake.result(firstId).outcome).toBe("abandoned");
    expect(fake.result(firstId).detail).toBe("lost delivery answer");
    expect(fake.registered).toEqual([entry()]);
    expect(fake.withdrawn).toEqual([]);
    outbox.cancel();
  });

  for (const badText of ["{", JSON.stringify({ id: firstId })]) {
    test(`an unreadable or incomplete entry does not stop the next entry: ${badText}`, async () => {
      const fake = fakeCalls();
      fake.files.set(path("outbox", firstId), badText);
      fake.put(entry(secondId));
      const outbox = await startOutbox(fake.calls, fake.replies);
      await outbox.poll();
      expect(fake.result(firstId).outcome).toBe("failed");
      expect(fake.result(firstId).detail).toBe("unreadable entry");
      expect(fake.registered.map(item => item.id)).toEqual([secondId]);
      expect(fake.result(secondId).outcome).toBe("delivered");
      expect(fake.sent.length).toBe(1);
      outbox.cancel();
    });
  }

  test("a file read failure produces unreadable entry and the next entry still goes", async () => {
    const fake = fakeCalls();
    fake.put(entry());
    fake.put(entry(secondId));
    fake.onBeforeRead(async filename => {
      if (filename === path("outbox", firstId)) throw new Error("entry cannot be read");
    });
    const outbox = await startOutbox(fake.calls, fake.replies);
    await outbox.poll();
    expect(fake.result(firstId).outcome).toBe("failed");
    expect(fake.result(firstId).detail).toBe("unreadable entry");
    expect(fake.result(secondId).outcome).toBe("delivered");
    expect(fake.sent.length).toBe(1);
    outbox.cancel();
  });

  test("a refused send keeps its reason and retries withdrawal without sending again", async () => {
    const fake = fakeCalls();
    fake.put(entry());
    fake.answerSend(async () => ({ isDelivered: false, reason: "rate limit" }));
    const attempts: RouteToken[] = [];
    fake.replies.notDelivered = async token => {
      attempts.push(token);
      if (attempts.length === 1) throw new Error("withdrawal write failed");
    };
    const outbox = await startOutbox(fake.calls, fake.replies);
    try {
      await outbox.poll();
      expect(fake.result(firstId).outcome).toBe("failed");
      expect(fake.result(firstId).detail).toBe("rate limit");
      expect([...fake.files.keys()].filter(filename => filename.startsWith(`${directory}/errors/`)).length).toBe(1);
      await outbox.poll();
      await outbox.poll();
      expect(attempts).toEqual([fake.tokens[0], fake.tokens[0]]);
      expect(fake.sent.length).toBe(1);
      expect(fake.result(firstId).detail).toBe("rate limit");
    } finally { outbox.cancel(); }
  });

  test("a failed result write is recorded and retried without repeating delivery", async () => {
    const fake = fakeCalls();
    fake.put(entry());
    let rejectResult = true;
    fake.onBeforeWrite(async filename => {
      if (filename === path("results", firstId) && rejectResult) {
        rejectResult = false;
        throw new Error("result write failed");
      }
    });
    const outbox = await startOutbox(fake.calls, fake.replies);
    await outbox.poll();
    expect(fake.sent.length).toBe(1);
    expect(fake.files.has(path("results", firstId))).toBe(false);
    const errors = [...fake.files.keys()].filter(filename => filename.startsWith(`${directory}/errors/`));
    expect(errors.length).toBe(1);
    const errorPath = errors[0];
    if (errorPath === undefined) throw new Error("Expected an error record");
    expect(JSON.stringify(record(fake.files, errorPath))).toContain("result write failed");
    await outbox.poll();
    expect(fake.sent.length).toBe(1);
    expect(fake.result(firstId).outcome).toBe("delivered");
    outbox.cancel();
  });

  test("a claim from an earlier worker without a result becomes abandoned without a send", async () => {
    const fake = fakeCalls();
    fake.put(entry());
    fake.files.set(path("claimed", firstId), JSON.stringify({ id: firstId, claimed_ms: 500, run: "earlier-worker" }));
    const outbox = await startOutbox(fake.calls, fake.replies);
    await outbox.poll();
    await outbox.poll();
    expect(fake.sent).toEqual([]);
    expect(fake.result(firstId).outcome).toBe("abandoned");
    expect(fake.result(firstId).claimed_ms).toBe(500);
    outbox.cancel();
  });

  test("an earlier worker's completed result is retained without sending", async () => {
    const fake = fakeCalls();
    fake.put(entry());
    fake.files.set(path("claimed", firstId), JSON.stringify({ id: firstId, claimed_ms: 500, run: "earlier-worker" }));
    const prior = { id: firstId, outcome: "delivered", detail: "earlier delivery", sha256: helloHash, claimed_ms: 500, finished_ms: 600 };
    fake.files.set(path("results", firstId), JSON.stringify(prior));
    const outbox = await startOutbox(fake.calls, fake.replies);
    await outbox.poll();
    expect(fake.sent).toEqual([]);
    expect(fake.result(firstId)).toEqual(prior);
    outbox.cancel();
  });

  test("heartbeat is rewritten on each poll and before every slow send", async () => {
    const fake = fakeCalls();
    const outbox = await startOutbox(fake.calls, fake.replies);
    await outbox.poll();
    expect(record(fake.files, `${directory}/heartbeat.json`).at_ms).toBe(1000);
    fake.moveNow(500);
    await outbox.poll();
    expect(record(fake.files, `${directory}/heartbeat.json`).at_ms).toBe(1500);
    const timestamps: number[] = [];
    for (let index = 1; index <= 20; index++) fake.put(entry(index.toString(16).padStart(32, "0")));
    fake.answerSend(async () => {
      const heartbeat = record(fake.files, `${directory}/heartbeat.json`);
      expect(heartbeat.session_id).toBe("postmaster-session");
      expect(typeof heartbeat.version).toBe("string");
      expect(heartbeat.at_ms).toBe(fake.calls.now());
      timestamps.push(fake.calls.now());
      fake.moveNow(1500);
      return { isDelivered: true };
    });
    await outbox.poll();
    expect(timestamps).toEqual(Array.from({ length: 20 }, (_, index) => 1500 + index * 1500));
    expect(fake.sent.length).toBe(20);
    outbox.cancel();
  });

  test("two entries for one session are sent in oldest file order", async () => {
    const fake = fakeCalls();
    fake.put({ ...entry(firstId), text: "second" });
    fake.put({ ...entry(secondId), text: "first" });
    fake.setFileTime(path("outbox", firstId), 20);
    fake.setFileTime(path("outbox", secondId), 10);
    const outbox = await startOutbox(fake.calls, fake.replies);
    await outbox.poll();
    expect(fake.sent.map(message => message.text)).toEqual(["first", "second"]);
    outbox.cancel();
  });

  test("dot files are never read and a poll sends at most twenty entries", async () => {
    const fake = fakeCalls();
    fake.files.set(`${directory}/outbox/.pending.json`, "not JSON");
    for (let index = 21; index >= 1; index--) fake.put(entry(index.toString(16).padStart(32, "0")));
    const outbox = await startOutbox(fake.calls, fake.replies);
    await outbox.poll();
    expect(fake.sent.length).toBe(20);
    expect(fake.reads.includes(`${directory}/outbox/.pending.json`)).toBe(false);
    await outbox.poll();
    expect(fake.sent.length).toBe(21);
    outbox.cancel();
  });

  test("a second poll cannot enter while a send is pending", async () => {
    const fake = fakeCalls();
    let finish: () => void = () => { throw new Error("Send not started"); };
    let signal: () => void = () => {};
    const sending = new Promise<void>(resolve => { signal = resolve; });
    const outbox = await startOutbox(fake.calls, fake.replies);
    fake.put(entry());
    fake.answerSend(() => new Promise<SessionSendResult>(resolve => {
      finish = () => { resolve({ isDelivered: true }); };
      signal();
    }));
    const firstPoll = outbox.poll();
    await sending;
    const writeCount = fake.writes.length;
    await outbox.poll();
    expect(fake.writes.length).toBe(writeCount);
    expect(fake.sent.length).toBe(1);
    finish();
    await firstPoll;
    expect(fake.result(firstId).outcome).toBe("delivered");
    outbox.cancel();
  });

  test("state directory uses HOME only when XDG_STATE_HOME is unset", async () => {
    const fake = fakeCalls();
    fake.environment.delete("XDG_STATE_HOME");
    const outbox = await startOutbox(fake.calls, fake.replies);
    await outbox.poll();
    expect(fake.files.has("/test-home/.local/state/message/postmaster/heartbeat.json")).toBe(true);
    expect(fake.files.has(`${directory}/heartbeat.json`)).toBe(false);
    outbox.cancel();
  });
});
