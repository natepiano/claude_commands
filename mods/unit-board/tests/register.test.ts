import { describe, expect, mock, test } from "claude-code/testing";
import type { CommandRunInput, TurnCompleteInput } from "claude-code";
import { refreshIfChanged } from "../hooks/data";
import { CWD, drawnText, hostEngine, NOW } from "./kit";

const command = (args = ""): CommandRunInput => ({ command: "showrunner:accounts", args,
  origin: { kind: "composer" }, presentation: { isFullscreen: false, columns: 170 } });
const start = { cwd: CWD, surface: null, isInteractive: false } as const;
const turn: TurnCompleteInput = { answer: "work finished", reason: "answer", durationMs: 100,
  isAborted: false, turnId: "test-turn" };
const props = { title: "Accounts", isFocused: true, bodyColumns: 100,
  scroll: { offset: 0, bodyRows: 60 }, view: {}, placement: "dock" as const };

describe("shared pane lifecycle", () => {
  test("startup registers no commands", async ($, on) => {
    mock.env(on, { HOME: "/test-home", USER_ZONE: "UTC" });
    const registered: string[] = [];
    on("session.start", (_$, event) => ({ cwd: event.cwd }));
    on("ui.panes", () => ({ value: [] }));
    on("command.register", (_$, event) => {
      registered.push(event.name);
      return { value: { command: event.name } };
    });

    await $.session.start(start);
    expect(registered).toEqual([]);
  });

  test("the retired accounts command does not open a pane", async ($, on) => {
    const host = hostEngine(on);
    const processes: string[][] = [];
    on("process.run", (_$, event) => {
      processes.push([...event.argv]);
      throw new Error("A closed pane must not run a process.");
    });
    on("command.run", () => ({ text: "Command not handled by the mod." }));
    expect(await $.command.run({ ...command(), command: "accounts-board" }))
      .toEqual({ text: "Command not handled by the mod." });
    expect(host.opened).toEqual([]);
    expect(processes).toEqual([]);
  });

  test("a closed pane never requests a timer at startup or turn completion", async ($, on) => {
    mock.env(on, { HOME: "/test-home" });
    hostEngine(on);
    const requests: number[] = [];
    on("clock.every", (_$, event) => {
      requests.push(event.ms);
      throw new Error("Closed pane requested a repeating timer");
    });
    on("clock.after", (_$, event) => {
      requests.push(event.ms);
      throw new Error("Closed pane requested a timer");
    });
    await $.session.start(start);
    await $.turn.complete(turn);
    expect(requests).toEqual([]);
  });

  test("closed startup and turn completion perform no file, process, or timer work", async ($, on) => {
    const host = hostEngine(on);
    const clock = mock.clock(on, { now: NOW });
    const calls: string[] = [];
    on("fs.read", () => { calls.push("read"); throw new Error("A closed pane must not read files."); });
    on("fs.stat", () => { calls.push("stat"); throw new Error("A closed pane must not inspect files."); });
    on("process.run", () => { calls.push("process"); throw new Error("A closed pane must not run a process."); });
    await $.session.start(start);
    await $.turn.complete({ answer: "finished", reason: "answer", durationMs: 100,
      isAborted: false, turnId: "closed-turn" });
    await clock.advance(60_000);
    expect(calls).toEqual([]);
    expect(host.opened).toEqual([]);
    expect(host.forbidden).toEqual([]);
  });

  test("the shared stat helper refreshes only the pane whose sources changed", async () => {
    const mtimes = new Map([["/a.json", 100], ["/a-source", 101],
      ["/b.json", 100], ["/b-source", 99]]);
    const services = { stat: async (path: string) => {
      const mtimeMs = mtimes.get(path);
      if (mtimeMs === undefined) throw new Error(`Missing ${path}`);
      return { mtimeMs };
    } };
    const generated: string[] = [];
    const states: string[] = [];
    const refresh = async (pane: string) => refreshIfChanged(services,
      [{ kind: "discovery", loadedAt: 100, sources: [`/${pane}-source`] }],
      async () => { generated.push(pane); return `${pane} refreshed`; },
      async value => { states.push(value); }, () => true);
    expect(await refresh("a")).toBe(true);
    expect(await refresh("b")).toBe(false);
    expect(generated).toEqual(["a"]);
    expect(states).toEqual(["a refreshed"]);
    mtimes.delete("/b-source");
    expect(await refresh("b")).toBe(true);
    expect(generated).toEqual(["a", "b"]);
    expect(states).toEqual(["a refreshed", "b refreshed"]);
  });

});

describe("accounts pane clock", () => {
  const accountsCommand: CommandRunInput = { ...command(), command: "showrunner:accounts" };

  function accountsClockHost(on: Parameters<typeof hostEngine>[0]) {
    const host = hostEngine(on);
    const clock = mock.clock(on, { now: NOW });
    mock.env(on, { HOME: "/test-home", USER_ZONE: "America/New_York" });
    const data = { accounts: [{ label: "Fixture", tool: "claude", roles: ["workers"],
      weekly_left_percent: 60, resets_at: "2026-10-17T03:59:55Z", credits: null,
      use_credits: false, read_ok: true, credits_in_use: false, read_at: "2026-10-17T03:59:55Z" }], problems: [] };
    const processes: string[][] = [];
    const reads: string[] = [];
    const snapshots: Record<string, unknown>[] = [];
    let sourceMtime = NOW;
    on("fs.stat", () => ({ value: { kind: "file", size: 0, mtimeMs: sourceMtime, isLink: false } }));
    on("fs.exists", () => ({ value: true }));
    on("fs.list", () => ({ value: [] }));
    on("fs.read", (_$, event) => { reads.push(event.path); throw new Error("Accounts clock reads no fixture files"); });
    on("process.run", (_$, event) => {
      processes.push([...event.argv]);
      return { value: { exitCode: 0, stdout: JSON.stringify(data), stderr: "",
        isStdoutTruncated: false, isStderrTruncated: false } };
    });
    on("state.set", { plugin: "unit-board", key: "accountsPanes" }, (_$, event, next) => {
      const state = event.value["accounts"];
      if (state?.kind === "accounts") snapshots.push({ ...state });
      return next(event);
    });
    return { ...host, clock, processes, reads, snapshots,
      change: () => { sourceMtime = clock.now() + 1; } };
  }

  test("load and every unchanged ten-second tick store fresh epoch seconds without another process", async ($, on) => {
    const fake = accountsClockHost(on);
    await fake.clock.advance(12_345);
    await $.command.run(accountsCommand);
    expect(fake.snapshots.at(-1)?.now).toBe((NOW + 12_345) / 1000);
    expect(fake.snapshots.at(-1)?.zone).toBe("America/New_York");
    const snapshotData = fake.snapshots.at(-1)?.data;
    expect(fake.processes.length).toBe(1);
    for (const elapsed of [10_000, 20_000]) {
      await fake.clock.advance(10_000);
      expect(fake.snapshots.at(-1)?.now).toBe((NOW + 12_345 + elapsed) / 1000);
      expect(fake.snapshots.at(-1)?.data).toEqual(snapshotData);
      expect(fake.processes.length).toBe(1);
      expect(fake.reads).toEqual([]);
    }
  });

  test("a watched-source reload stamps its new reading with the current clock", async ($, on) => {
    const fake = accountsClockHost(on);
    await $.command.run(accountsCommand);
    fake.change();
    await fake.clock.advance(10_000);
    expect(fake.processes.length).toBe(2);
    expect(fake.snapshots.at(-1)?.now).toBe((NOW + 10_000) / 1000);
    expect(fake.reads).toEqual([]);
  });

  for (const surface of ["terminal", "desktop"] as const) {
    test(`${surface}: an unchanged tick across local midnight redraws resets from the new now`, async ($, on) => {
      const fake = accountsClockHost(on);
      const midnight = Date.UTC(2026, 9, 17, 4, 0);
      await fake.clock.advance(midnight - NOW - 5_000);
      await $.command.run(accountsCommand);
      const ui = await $.ui.mount({ plugin: "unit-board", requestId: "accounts", component: "Pane", surface,
        props: { ...props, title: "Accounts", bodyColumns: 120 } });
      const before = drawnText(await ui.drawn()).replace(/\s+/g, " ");
      expect(before).toContain("account left role busy starting waiting idle paused exited total resets credits enabled");
      expect(before).toMatch(/● Fixture 60% workers 23:59 total 0 0 0 0 0 0 0\s*$/);
      await fake.clock.advance(10_000);
      const after = drawnText(await ui.drawn()).replace(/\s+/g, " ");
      expect(after).toContain("account left role busy starting waiting idle paused exited total resets credits enabled");
      expect(after).toMatch(/● Fixture 60% workers Fri 23:59 total 0 0 0 0 0 0 0\s*$/);
      expect(fake.snapshots.at(-1)?.now).toBe((midnight + 5_000) / 1000);
      expect(fake.processes.length).toBe(1);
      expect(fake.reads).toEqual([]);
    });
  }
});
