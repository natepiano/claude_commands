import type { Register } from "claude-code";
import { start, receive, failed } from "./probe";
import type { SessionSendArgs } from "claude-code";

export const register: Register = (on) => {
  on("session.start", async ($, e, next) => {
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
          list: (path?: string) => $.fs.list(path),
        },
        session: {
          id: () => $.session.id(),
          send: (message: SessionSendArgs) => $.session.send(message),
        },
      }, e);
    } catch (error) { await failed(error); }
    return next(e);
  }).catch(async (_$, e, next) => {
    await failed(e);
    return next(e);
  });
  on("session.receive", async (_$, e, next) => {
    try {
      const result = await receive(e);
      if (result.status === "consumed") return { consumed: result.reason };
    } catch (error) { await failed(error); }
    return next(e);
  }).catch(async (_$, e, next) => {
    await failed(e);
    return next(e);
  });
};
