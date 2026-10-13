import type { Register, SessionSendArgs } from "claude-code";
import { startOutbox, reportOutboxError } from "./outbox";
import type { Outbox, PostmasterCalls } from "./protocol";

type ModuleTimers = {
  setInterval(fn: () => void, ms: number): unknown;
  clearInterval(handle: unknown): void;
};
function isModuleTimers(value: unknown): value is ModuleTimers {
  return typeof value === "object" && value !== null
    && "setInterval" in value && typeof value.setInterval === "function"
    && "clearInterval" in value && typeof value.clearInterval === "function";
}

export const register: Register = (on) => {
  let worker: { status: "waiting" } | { status: "started"; calls: PostmasterCalls; outbox: Promise<Outbox> } = { status: "waiting" };
  on("session.start", async ($, e, next) => {
    if (worker.status === "waiting") {
      const calls: PostmasterCalls = {
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
      worker = { status: "started", calls, outbox: startOutbox(calls) };
    }
    const active = worker;
    await active.outbox.catch(async error => {
      await reportOutboxError(active.calls, error);
      worker = { status: "waiting" };
    });
    return next(e);
  }).catch(async (_$, e, next) => {
    if (worker.status === "started") await reportOutboxError(worker.calls, e);
    return next(e);
  });
};
