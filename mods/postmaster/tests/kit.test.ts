import { describe, expect, mock, test } from "claude-code/testing";
import type { Plugin } from "claude-code/testing";
import type { FsEntry, SessionSendInput } from "claude-code";

describe("mod test kit", () => {
  test("fake clock holds a hook wait until advanced", {
    plugins: [{
      name: "kit-clock",
      register(on) {
        on("session.receive", { text: "kit-clock" }, async ($) => {
          await $.clock.sleep(500);
          return { consumed: "clock wait finished" };
        });
      },
    } satisfies Plugin],
  }, async ($, on) => {
    const clock = mock.clock(on, { now: 1000 });
    let finished = false;
    const delivery = $.session.receive({ origin: { kind: "peer" }, text: "kit-clock" });
    void delivery.then(() => { finished = true; });
    await clock.settle();
    expect(finished).toBe(false);
    await clock.advance(499);
    expect(finished).toBe(false);
    await clock.advance(1);
    expect(finished).toBe(true);
    expect(await delivery).toEqual({ consumed: "clock wait finished" });
    expect(clock.now()).toBe(1500);
  });

  test("test hooks answer and record session send", {
    plugins: [{
      name: "kit-send",
      register(on) {
        on("session.receive", { text: "kit-send" }, async ($) => {
          const result = await $.session.send({ to: { sessionId: "fake-peer" }, text: "kit message" });
          return { consumed: result.isDelivered ? "fake delivered" : result.reason };
        });
      },
    } satisfies Plugin],
  }, async ($, on) => {
    const sent: SessionSendInput[] = [];
    on("session.send", (_$, e) => {
      sent.push(e);
      return { isDelivered: true };
    });
    expect(await $.session.receive({ origin: { kind: "peer" }, text: "kit-send" }))
      .toEqual({ consumed: "fake delivered" });
    expect(sent.length).toBe(1);
    expect(sent[0]?.to).toBe("fake-peer");
    expect(sent[0]?.text).toBe("kit message");
  });

  test("test hooks answer file write list read and exists from memory", {
    plugins: [{
      name: "kit-files",
      register(on) {
        on("session.receive", { text: "kit-files" }, async ($) => {
          await $.fs.write("/kit-memory/file.txt", "kit file");
          const entries = await $.fs.list("/kit-memory");
          const text = await $.fs.read("/kit-memory/file.txt");
          const exists = await $.fs.exists("/kit-memory/file.txt");
          return { consumed: JSON.stringify({ entries, text, exists }) };
        });
      },
    } satisfies Plugin],
  }, async ($, on) => {
    const files = new Map<string, string>();
    const calls: string[] = [];
    on("fs.write", (_$, e) => {
      calls.push("write");
      files.set(e.path, e.text);
      return { value: undefined };
    });
    on("fs.list", (_$, e) => {
      calls.push("list");
      const entries: FsEntry[] = [];
      for (const [path, text] of files) {
        if (path.startsWith(e.path + "/")) {
          entries.push({ name: path.slice(e.path.length + 1), kind: "file", size: text.length, mtimeMs: 0, isLink: false });
        }
      }
      return { value: entries };
    });
    on("fs.read", (_$, e) => {
      calls.push("read");
      const text = files.get(e.path);
      if (text === undefined) throw new Error("No file in the test memory");
      if (e.as !== "text") throw new Error("This test fake reads text only");
      return { value: text };
    });
    on("fs.exists", (_$, e) => {
      calls.push("exists");
      return { value: files.has(e.path) };
    });
    const result = await $.session.receive({ origin: { kind: "peer" }, text: "kit-files" });
    expect(result).toEqual({ consumed: JSON.stringify({
      entries: [{ name: "file.txt", kind: "file", size: 8, mtimeMs: 0, isLink: false }], text: "kit file", exists: true,
    }) });
    expect(calls).toEqual(["write", "list", "read", "exists"]);
    expect(files.get("/kit-memory/file.txt")).toBe("kit file");
  });

  test("raised receive is consumed without next and passes with next", {
    plugins: [{
      name: "kit-receive",
      register(on) {
        on("session.receive", (_$, e, next) => {
          if (e.text === "kit-consume") return { consumed: "test consumed" };
          return next(e);
        });
      },
    } satisfies Plugin],
  }, async ($, on) => {
    const queued: string[] = [];
    on("session.receive", (_$, e) => {
      queued.push(e.text);
      return { text: e.text };
    });
    expect(await $.session.receive({ origin: { kind: "peer" }, text: "kit-consume" }))
      .toEqual({ consumed: "test consumed" });
    expect(queued).toEqual([]);
    expect(await $.session.receive({ origin: { kind: "peer" }, text: "kit-pass" }))
      .toEqual({ text: "kit-pass" });
    expect(queued).toEqual(["kit-pass"]);
  });
});
