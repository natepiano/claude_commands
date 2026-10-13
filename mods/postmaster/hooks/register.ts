import type { Register } from "claude-code";
import { start, receive, failed } from "./probe";
import type { ProbeCalls, SessionDestination } from "./probe";

type Continuation = (event: unknown) => unknown;
type HookHandler = (context: ProbeCalls, event: unknown, next: Continuation) => Promise<unknown>;
type HookRegistration = {
  catch(handler: HookHandler): void;
};
type HookRegistrar = (event: "session.start" | "session.receive", handler: HookHandler) => HookRegistration;

export const register: Register = (on: HookRegistrar) => {
  on("session.start", async ($: ProbeCalls, e: unknown, next: Continuation) => {
    try {
      await start({
        env: { get: (name: string) => {
          if (name === "HOME") return $.env.get("HOME");
          if (name === "XDG_STATE_HOME") return $.env.get("XDG_STATE_HOME");
          return Promise.reject(new Error(`Unsupported environment name: ${name}`));
        } },
        fs: {
          read: (path: string) => $.fs.read(path),
          write: (path: string, text: string) => $.fs.write(path, text),
          exists: (path: string) => $.fs.exists(path),
          list: (path: string) => $.fs.list(path),
        },
        session: {
          id: () => $.session.id(),
          send: (message: { to: SessionDestination; text: string }) => $.session.send(message),
        },
      }, e);
    } catch (error) { await failed(error); }
    return next(e);
  }).catch(async (_$: unknown, e: unknown, next: Continuation) => {
    await failed(e);
    return next(e);
  });
  on("session.receive", async (_$: unknown, e: unknown, next: Continuation) => {
    try {
      const result = await receive(e);
      if (result.status === "consumed") return { consumed: result.reason };
    } catch (error) { await failed(error); }
    return next(e);
  }).catch(async (_$: unknown, e: unknown, next: Continuation) => {
    await failed(e);
    return next(e);
  });
};
