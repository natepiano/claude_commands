import type { EngineInterface, Register, SessionSendArgs } from "claude-code";
import { startOutbox, reportOutboxError } from "./outbox";
import { startReplies } from "./replies";
import type { Outbox, PostmasterCalls, Replies } from "./protocol";

type ModuleTimers = {
  setInterval(fn: () => void, ms: number): unknown;
  clearInterval(handle: unknown): void;
};
type WorkerServices = { outbox: Outbox; replies: Replies };
type Worker = { status: "waiting" } | { status: "started"; calls: PostmasterCalls; services: Promise<WorkerServices> };

function isModuleTimers(value: unknown): value is ModuleTimers {
  return typeof value === "object" && value !== null
    && "setInterval" in value && typeof value.setInterval === "function"
    && "clearInterval" in value && typeof value.clearInterval === "function";
}

function callsFor($: EngineInterface): PostmasterCalls {
  return {
    env: { get: (name: string) => {
      if (name === "HOME") return $.env.get("HOME");
      if (name === "XDG_STATE_HOME") return $.env.get("XDG_STATE_HOME");
      return Promise.reject(new Error(`Unsupported environment name: ${name}`));
    } },
    fs: {
      read: (path: string) => $.fs.read(path),
      write: (path: string, text: string) => $.fs.write(path, text),
      exists: (path: string) => $.fs.exists(path),
      list: (path?: string) => $.fs.list(path),
    },
    session: {
      id: () => $.session.id(),
      send: (message: SessionSendArgs) => $.session.send(message),
    },
    now: Date.now,
    every: (ms, fn) => {
      const timers: unknown = globalThis;
      if (!isModuleTimers(timers)) throw new Error("Module interval timers are unavailable");
      const interval = timers.setInterval(fn, ms);
      return { cancel: () => timers.clearInterval(interval) };
    },
  };
}

export const register: Register = (on) => {
  let worker: Worker = { status: "waiting" };
  const startWorker = (calls: PostmasterCalls): Promise<WorkerServices> => {
    if (worker.status === "started") return worker.services;
    const services = startReplies(calls).then(replies => startOutbox(calls, replies).then(outbox => ({ outbox, replies })));
    const active: Extract<Worker, { status: "started" }> = {
      status: "started", calls,
      services: services.catch(async error => {
        await reportOutboxError(calls, error);
        if (worker === active) worker = { status: "waiting" };
        throw error;
      }),
    };
    worker = active;
    return active.services;
  };

  on("session.start", async ($, e, next) => {
    await startWorker(callsFor($)).catch(() => {});
    return next(e);
  }).catch(async ($, e, next) => {
    await reportOutboxError(callsFor($), e);
    return next(e);
  });

  on("session.receive", async ($, e, next) => {
    try {
      const services = await startWorker(callsFor($));
      const reply = await services.replies.receive(e);
      if (reply.kind === "consumed") return { consumed: reply.reason };
    } catch { /* A failed start or reply handler leaves the message for the session. */ }
    return next(e);
  }).catch(async ($, e, next) => {
    await reportOutboxError(callsFor($), e);
    return next(e);
  });
};
