import { describe, expect, test } from "claude-code/testing";
import type { FsEntry, SessionReceiveInput, SessionSendArgs, SessionSendResult } from "claude-code";
import type { DeliveryRouteFile, ForwardLog, OutboxEntry, PostmasterCalls, ReplyLog } from "../hooks/protocol";
import { isDeliveryRouteFile, isForwardLog, isReplyLog, REPLY_WINDOW_MS } from "../hooks/protocol";
import { startReplies } from "../hooks/replies";
import { startOutbox } from "../hooks/outbox";

const directory = "/test-state/message/postmaster";
const socket = "uds:/test-sockets/recipient.sock";
const consumed = { kind: "consumed", reason: "postmaster reply" };

function entry(sender = "Original sender", destination = "original-session"): OutboxEntry {
  return { id: "00000000000000000000000000000001", session_id: "recipient", socket,
    sender, reply_to_session_id: destination, text: "question", sha256: "a".repeat(64), deadline_ms: 100000 };
}

function reply(body = "My answer", from = socket): SessionReceiveInput {
  return { origin: { kind: "peer" }, text:
    `<cross-session-message from="${from}" from-name="Recipient" from-mode="auto" from-plugin="postmaster">\n${body}\n</cross-session-message>` };
}

function route(at_ms = 1000): DeliveryRouteFile {
  return { route: "open", socket, sender: "Original sender", reply_to_session_id: "original-session",
    session_id: "recipient", at_ms };
}

function fakeCalls() {
  const files = new Map<string, string>();
  const writes: { path: string; text: string }[] = [];
  const sent: SessionSendArgs[] = [];
  let now = 1000;
  let sendAnswer: (message: SessionSendArgs) => Promise<SessionSendResult> = async () => ({ isDelivered: true });
  const calls: PostmasterCalls = {
    env: { get: async name => name === "HOME" ? "/test-home" : name === "XDG_STATE_HOME" ? "/test-state" : undefined },
    fs: {
      read: async path => {
        const text = files.get(path);
        if (text === undefined) throw new Error(`No test file: ${path}`);
        return text;
      },
      write: async (path, text) => { files.set(path, text); writes.push({ path, text }); },
      exists: async path => path === `${directory}/delivered` || files.has(path) || [...files.keys()].some(file => file.startsWith(path + "/")),
      list: async (path = directory) => {
        const entries: FsEntry[] = [];
        for (const [file, text] of files) {
          if (!file.startsWith(path + "/")) continue;
          const name = file.slice(path.length + 1);
          if (!name.includes("/")) entries.push({ name, kind: "file", size: text.length, mtimeMs: 1000, isLink: false });
        }
        return entries;
      },
    },
    session: { id: async () => "postmaster-session", send: async message => { sent.push(message); return sendAnswer(message); } },
    now: () => now,
    every: () => ({ cancel() {} }),
  };
  function logs(): ReplyLog[] {
    return [...files].filter(([path]) => path.startsWith(`${directory}/replies/`)).map(([, text]) => {
      const value: unknown = JSON.parse(text);
      if (!isReplyLog(value)) throw new Error("Invalid reply log");
      return value;
    });
  }
  function forwards(): ForwardLog[] {
    return [...files].filter(([path]) => path.startsWith(`${directory}/forwards/`)).map(([, text]) => {
      const value: unknown = JSON.parse(text);
      if (!isForwardLog(value)) throw new Error("Invalid forward log");
      return value;
    });
  }
  function routeFile(key: string): DeliveryRouteFile {
    const text = files.get(`${directory}/delivered/${key}.json`);
    if (text === undefined) throw new Error("Missing route file");
    const value: unknown = JSON.parse(text);
    if (!isDeliveryRouteFile(value)) throw new Error("Invalid route file");
    return value;
  }
  return { calls, files, writes, sent, logs, forwards, routeFile,
    moveNow: (ms: number) => { now += ms; },
    answerSend: (answer: typeof sendAnswer) => { sendAnswer = answer; },
  };
}

function firstLine(message: SessionSendArgs): string {
  return message.text.split("\n")[0] ?? "";
}

function onlyForwarded(forwards: ForwardLog[]): string[] {
  expect(forwards.length).toBe(1);
  const forward = forwards[0];
  if (forward === undefined) throw new Error("Expected one forward log");
  return forward.forwarded;
}

function onlyLog(logs: ReplyLog[]): ReplyLog {
  expect(logs.length).toBe(1);
  const log = logs[0];
  if (log === undefined) throw new Error("Expected one reply log");
  return log;
}

describe("reply routes", () => {
  test("a delivered-to peer reply is consumed logged and forwarded with its original body", async () => {
    const fake = fakeCalls();
    const replies = await startReplies(fake.calls);
    await replies.sending(entry(), fake.calls.now());
    const body = "My answer\n\nincluding a second paragraph";
    expect(await replies.receive(reply(body))).toEqual(consumed);
    expect(fake.sent.length).toBe(1);
    const forward = fake.sent[0];
    if (forward === undefined) throw new Error("Expected a forward");
    expect(forward.to).toEqual({ sessionId: "original-session" });
    expect(firstLine(forward)).toEqual(expect.stringMatching(/^Message from Recipient \(their answer to Original sender, passed on by postmaster\) \[1000-\d+\]:$/));
    expect(forward.text.slice(forward.text.indexOf("\n") + 1)).toBe("\n" + body);
    expect(onlyLog(fake.logs())).toEqual({ at_ms: 1000, socket, name: "Recipient", session_id: "recipient",
      senders: ["Original sender"], body, targets: ["original-session"] });
    expect(onlyForwarded(fake.forwards())).toEqual(["original-session"]);
  });

  test("the reply record is complete while the forward is pending and no record file is written twice", async () => {
    const fake = fakeCalls();
    const replies = await startReplies(fake.calls);
    await replies.sending(entry(), fake.calls.now());
    let began: () => void = () => {};
    const forwarding = new Promise<void>(resolve => { began = resolve; });
    let finish: () => void = () => { throw new Error("Forward not started"); };
    fake.answerSend(() => new Promise<SessionSendResult>(resolve => {
      finish = () => { resolve({ isDelivered: true }); };
      began();
    }));
    const receiving = replies.receive(reply());
    await forwarding;
    expect(onlyLog(fake.logs())).toEqual({ at_ms: 1000, socket, name: "Recipient", session_id: "recipient",
      senders: ["Original sender"], body: "My answer", targets: ["original-session"] });
    expect(fake.forwards()).toEqual([]);
    finish();
    expect(await receiving).toEqual(consumed);
    expect(onlyForwarded(fake.forwards())).toEqual(["original-session"]);
    const records = fake.writes.map(write => write.path).filter(path => /\/(replies|forwards)\//.test(path));
    expect(records.length).toBe(2);
    expect(new Set(records).size).toBe(2);
  });

  test("a reply arriving before the original send returns already has a route", async () => {
    const fake = fakeCalls();
    const replies = await startReplies(fake.calls);
    const item = entry();
    fake.files.set(`${directory}/outbox/${item.id}.json`, JSON.stringify(item));
    let began: () => void = () => {};
    const sending = new Promise<void>(resolve => { began = resolve; });
    let finish: () => void = () => { throw new Error("Original send not started"); };
    fake.answerSend(message => {
      if (typeof message.to !== "string" && "sessionId" in message.to && message.to.sessionId === "recipient") {
        return new Promise<SessionSendResult>(resolve => {
          finish = () => { resolve({ isDelivered: true }); };
          began();
        });
      }
      return Promise.resolve({ isDelivered: true });
    });
    const outbox = await startOutbox(fake.calls, replies);
    const poll = outbox.poll();
    try {
      await sending;
      expect(await replies.receive(reply())).toEqual(consumed);
      expect(onlyForwarded(fake.forwards())).toEqual(["original-session"]);
    } finally { finish(); await poll; outbox.cancel(); }
  });

  test("receives before and after another delivery retain all routes and deduplicate destinations", async () => {
    const fake = fakeCalls();
    const replies = await startReplies(fake.calls);
    await replies.sending(entry(), fake.calls.now());
    await replies.sending(entry(), fake.calls.now());
    expect(await replies.receive(reply("first"))).toEqual(consumed);
    await replies.sending(entry("Bob", "bob-session"), fake.calls.now());
    expect(await replies.receive(reply("second"))).toEqual(consumed);
    expect(fake.sent.map(message => message.to)).toEqual([
      { sessionId: "original-session" }, { sessionId: "original-session" }, { sessionId: "bob-session" },
    ]);
    expect(fake.logs().map(log => log.body)).toEqual(["first", "second"]);
  });

  test("non-peer origins plain text and unknown sockets pass without writes or forwards", async () => {
    const fake = fakeCalls();
    const replies = await startReplies(fake.calls);
    await replies.sending(entry(), fake.calls.now());
    const events: SessionReceiveInput[] = [
      { ...reply(), origin: { kind: "task-notification" } },
      { ...reply(), origin: { kind: "bridge" } },
      { ...reply(), origin: { kind: "coordinator" } },
      { origin: { kind: "peer" }, text: "ordinary message" },
      reply("unrelated message", "uds:/test-sockets/unknown.sock"),
    ];
    const before = fake.writes.length;
    for (const event of events) {
      const original = JSON.stringify(event);
      expect(await replies.receive(event)).toEqual({ kind: "pass" });
      expect(JSON.stringify(event)).toBe(original);
    }
    expect(fake.writes.length).toBe(before);
    expect(fake.sent).toEqual([]);
    expect(fake.logs()).toEqual([]);
  });

  test("a reply outside the thirty minute window passes", async () => {
    expect(REPLY_WINDOW_MS).toBe(30 * 60 * 1000);
    const fake = fakeCalls();
    const replies = await startReplies(fake.calls);
    await replies.sending(entry(), fake.calls.now());
    fake.moveNow(REPLY_WINDOW_MS + 1);
    expect(await replies.receive(reply())).toEqual({ kind: "pass" });
    expect(fake.logs()).toEqual([]);
    expect(fake.sent).toEqual([]);
  });

  test("an unresolved script sender is consumed and logged with no destination", async () => {
    const fake = fakeCalls();
    const replies = await startReplies(fake.calls);
    await replies.sending(entry("quota_alert", ""), fake.calls.now());
    expect(await replies.receive(reply())).toEqual(consumed);
    expect(onlyLog(fake.logs())).toEqual({ at_ms: 1000, socket, name: "Recipient", session_id: "recipient",
      senders: ["quota_alert"], body: "My answer", targets: [] });
    expect(onlyForwarded(fake.forwards())).toEqual([]);
    expect(fake.sent).toEqual([]);
  });

  test("a refused forward leaves its id out of the log", async () => {
    const fake = fakeCalls();
    const replies = await startReplies(fake.calls);
    await replies.sending(entry(), fake.calls.now());
    fake.answerSend(async () => ({ isDelivered: false, reason: "session is gone" }));
    expect(await replies.receive(reply())).toEqual(consumed);
    expect(fake.sent.length).toBe(1);
    expect(onlyLog(fake.logs()).targets).toEqual(["original-session"]);
    expect(onlyForwarded(fake.forwards())).toEqual([]);
  });

  test("a throwing forward still consumes the reply and logs an empty forwarded list", async () => {
    const fake = fakeCalls();
    const replies = await startReplies(fake.calls);
    await replies.sending(entry(), fake.calls.now());
    fake.answerSend(async () => { throw new Error("forward cannot be confirmed"); });
    expect(await replies.receive(reply())).toEqual(consumed);
    expect(onlyLog(fake.logs()).targets).toEqual(["original-session"]);
    expect(onlyForwarded(fake.forwards())).toEqual([]);
  });

  test("equal answers at the same time have distinct first lines and log files", async () => {
    const fake = fakeCalls();
    const replies = await startReplies(fake.calls);
    await replies.sending(entry(), fake.calls.now());
    expect(await replies.receive(reply())).toEqual(consumed);
    expect(await replies.receive(reply())).toEqual(consumed);
    expect(fake.sent.length).toBe(2);
    expect(new Set(fake.sent.map(firstLine)).size).toBe(2);
    expect(fake.logs().map(log => log.body)).toEqual(["My answer", "My answer"]);
  });

  test("two senders to one socket are logged in order and named in each forward", async () => {
    const fake = fakeCalls();
    const replies = await startReplies(fake.calls);
    await replies.sending(entry("Alice", "alice-session"), fake.calls.now());
    await replies.sending(entry("Bob", "bob-session"), fake.calls.now());
    expect(await replies.receive(reply())).toEqual(consumed);
    expect(fake.sent.map(message => message.to)).toEqual([{ sessionId: "alice-session" }, { sessionId: "bob-session" }]);
    for (const message of fake.sent) {
      expect(firstLine(message)).toEqual(expect.stringMatching(/^Message from Recipient \(their answer to one of Alice, Bob, passed on by postmaster\) \[1000-\d+\]:$/));
    }
    expect(onlyForwarded(fake.forwards())).toEqual(["alice-session", "bob-session"]);
    expect(onlyLog(fake.logs()).senders).toEqual(["Alice", "Bob"]);
  });

  test("each route has its own file and adding a route does not rewrite an earlier one", async () => {
    const fake = fakeCalls();
    const replies = await startReplies(fake.calls);
    const first = await replies.sending(entry(), fake.calls.now());
    expect(first.key).toEqual(expect.stringMatching(/^1000-\d+$/));
    const firstText = fake.files.get(`${directory}/delivered/${first.key}.json`);
    expect(fake.routeFile(first.key)).toEqual(route());
    const before = fake.writes.length;
    const second = await replies.sending(entry("Other sender", "other-session"), fake.calls.now());
    expect(second.key === first.key).toBe(false);
    expect(fake.writes.slice(before)).toEqual([{ path: `${directory}/delivered/${second.key}.json`, text: fake.files.get(`${directory}/delivered/${second.key}.json`) }]);
    expect(fake.files.get(`${directory}/delivered/${first.key}.json`)).toBe(firstText);
  });

  test("start restores recent open routes and skips withdrawn malformed invalid and old files", async () => {
    const fake = fakeCalls();
    fake.moveNow(REPLY_WINDOW_MS + 10);
    const states: { key: string; text: string; accepted: boolean }[] = [
      { key: "open", text: JSON.stringify(route(fake.calls.now() - 1)), accepted: true },
      { key: "withdrawn", text: JSON.stringify({ ...route(fake.calls.now()), route: "withdrawn" }), accepted: false },
      { key: "malformed", text: "{", accepted: false },
      { key: "invalid", text: JSON.stringify({ ...route(fake.calls.now()), sender: 17 }), accepted: false },
      { key: "old", text: JSON.stringify(route(0)), accepted: false },
    ];
    for (const state of states) {
      const routeSocket = `uds:/test-sockets/${state.key}.sock`;
      const text = state.text.replace(socket, routeSocket);
      fake.files.set(`${directory}/delivered/${fake.calls.now()}-${states.indexOf(state) + 1}.json`, text);
    }
    const before = new Map(fake.files);
    const replies = await startReplies(fake.calls);
    expect(fake.files).toEqual(before);
    for (const state of states) {
      expect(await replies.receive(reply("restored answer", `uds:/test-sockets/${state.key}.sock`)))
        .toEqual(state.accepted ? consumed : { kind: "pass" });
    }
    expect(fake.sent.length).toBe(1);
    expect(onlyForwarded(fake.forwards())).toEqual(["original-session"]);
  });

  test("rolling back a second route leaves the first route and its open file intact", async () => {
    const fake = fakeCalls();
    const replies = await startReplies(fake.calls);
    const first = await replies.sending(entry(), fake.calls.now());
    const firstText = fake.files.get(`${directory}/delivered/${first.key}.json`);
    const second = await replies.sending(entry(), fake.calls.now());
    await replies.notDelivered(second);
    expect(fake.files.get(`${directory}/delivered/${first.key}.json`)).toBe(firstText);
    expect(fake.routeFile(first.key)).toEqual(route());
    expect(fake.routeFile(second.key)).toEqual({ ...route(), route: "withdrawn" });
    expect(await replies.receive(reply())).toEqual(consumed);
    expect(onlyForwarded(fake.forwards())).toEqual(["original-session"]);
    expect(fake.sent.length).toBe(1);
  });

  test("a withdrawn route alone no longer recognizes a reply", async () => {
    const fake = fakeCalls();
    const replies = await startReplies(fake.calls);
    await replies.notDelivered(await replies.sending(entry(), fake.calls.now()));
    expect(await replies.receive(reply())).toEqual({ kind: "pass" });
    expect(fake.logs()).toEqual([]);
    expect(fake.sent).toEqual([]);
  });

  test("an outbox refusal withdraws only its route after an earlier delivery to the same socket", async () => {
    const fake = fakeCalls();
    const replies = await startReplies(fake.calls);
    const first = entry();
    fake.files.set(`${directory}/outbox/${first.id}.json`, JSON.stringify(first));
    let deliveries = 0;
    fake.answerSend(async message => {
      if (typeof message.to !== "string" && "sessionId" in message.to && message.to.sessionId === "recipient") {
        deliveries++;
        return deliveries === 1 ? { isDelivered: true } : { isDelivered: false, reason: "rate limit" };
      }
      return { isDelivered: true };
    });
    const outbox = await startOutbox(fake.calls, replies);
    try {
      await outbox.poll();
      const delivered = [...fake.files].filter(([path]) => path.startsWith(`${directory}/delivered/`));
      expect(delivered.length).toBe(1);
      const prior = delivered[0];
      if (prior === undefined) throw new Error("Expected the delivered route");
      const second = { ...entry(), id: "00000000000000000000000000000002" };
      fake.files.set(`${directory}/outbox/${second.id}.json`, JSON.stringify(second));
      await outbox.poll();
      expect(fake.files.get(prior[0])).toBe(prior[1]);
      const routes = [...fake.files].filter(([path]) => path.startsWith(`${directory}/delivered/`));
      expect(routes.length).toBe(2);
      const states = routes.map(([, text]) => {
        const value: unknown = JSON.parse(text);
        if (!isDeliveryRouteFile(value)) throw new Error("Invalid route file");
        return value.route;
      });
      expect(states).toEqual(["open", "withdrawn"]);
      expect(await replies.receive(reply())).toEqual(consumed);
      expect(onlyForwarded(fake.forwards())).toEqual(["original-session"]);
    } finally { outbox.cancel(); }
  });

  test("a failed withdrawal leaves no active route and a later poll prevents its restoration", async () => {
    const fake = fakeCalls();
    const replies = await startReplies(fake.calls);
    const item = entry();
    fake.files.set(`${directory}/outbox/${item.id}.json`, JSON.stringify(item));
    fake.answerSend(async () => ({ isDelivered: false, reason: "recipient is gone" }));
    const write = fake.calls.fs.write;
    let failures = 0;
    fake.calls.fs.write = async (path, text) => {
      if (path.startsWith(`${directory}/delivered/`) && text.includes('"route":"withdrawn"') && failures++ === 0) {
        throw new Error("withdrawal write failed");
      }
      await write(path, text);
    };
    const outbox = await startOutbox(fake.calls, replies);
    try {
      await outbox.poll();
      const saved = fake.files.get(`${directory}/results/${item.id}.json`);
      if (saved === undefined) throw new Error("Expected the refused-send result");
      expect(saved).toContain('"outcome":"failed"');
      expect(saved).toContain('"detail":"recipient is gone"');
      const paths = [...fake.files.keys()].filter(path => path.startsWith(`${directory}/delivered/`));
      expect(paths.length).toBe(1);
      const routePath = paths[0];
      if (routePath === undefined) throw new Error("Expected the route file");
      expect(fake.files.get(routePath)).toContain('"route":"open"');
      expect(await replies.receive(reply("unrelated message"))).toEqual({ kind: "pass" });
      expect(fake.logs()).toEqual([]);
      await outbox.poll();
      expect(fake.files.get(routePath)).toContain('"route":"withdrawn"');
      expect(fake.sent.length).toBe(1);
      expect([...fake.files.keys()].filter(path => path.startsWith(`${directory}/errors/`)).length).toBe(1);
      const restored = await startReplies(fake.calls);
      expect(await restored.receive(reply("unrelated message"))).toEqual({ kind: "pass" });
      expect(fake.logs()).toEqual([]);
      expect(fake.sent.length).toBe(1);
    } finally { outbox.cancel(); }
  });

  test("an abandoned outbox send retains its route for a later reply", async () => {
    const fake = fakeCalls();
    const replies = await startReplies(fake.calls);
    const item = entry();
    fake.files.set(`${directory}/outbox/${item.id}.json`, JSON.stringify(item));
    fake.answerSend(async message => {
      if (typeof message.to !== "string" && "sessionId" in message.to && message.to.sessionId === "recipient") {
        throw new Error("delivery answer lost");
      }
      return { isDelivered: true };
    });
    const outbox = await startOutbox(fake.calls, replies);
    try {
      await outbox.poll();
      expect(await replies.receive(reply())).toEqual(consumed);
      expect(onlyForwarded(fake.forwards())).toEqual(["original-session"]);
      const result = fake.files.get(`${directory}/results/${item.id}.json`);
      if (result === undefined) throw new Error("Expected the outbox result");
      expect(result).toContain('"outcome":"abandoned"');
    } finally { outbox.cancel(); }
  });
});
