import { describe, expect, mock, test } from "claude-code/testing";
import type { Engine, Mounted } from "claude-code/testing";
import type { CommandRunInput, RenderSurface, TurnCompleteInput } from "claude-code";
import { refreshIfChanged } from "../hooks/data";
import { BOARD, CWD, DISCOVERY, drawnText, engineFiles, fixture, hostEngine, lifecycleHarness, NOW, REGISTRY, RUN, SESSION, time } from "./kit";

const command = (args = ""): CommandRunInput => ({ command: "showrunner:unit_board", args,
  origin: { kind: "composer" }, presentation: { isFullscreen: false, columns: 170 } });
const start = { cwd: CWD, surface: null, isInteractive: false } as const;
const turn: TurnCompleteInput = { answer: "work finished", reason: "answer", durationMs: 100,
  isAborted: false, turnId: "test-turn" };
const props = { title: "Units", isFocused: true, bodyColumns: 100,
  scroll: { offset: 0, bodyRows: 60 }, view: {}, placement: "dock" as const };
async function mount($: Engine, surface: "terminal" | "desktop") {
  return $.ui.mount({ plugin: "unit-board", requestId: "unit-board", component: "Pane", surface, props });
}
async function press<Surface extends RenderSurface>(ui: Mounted<Surface, "Pane">, label: string) {
  const button = (await ui.findAll({ type: "Button" })).find(value => value.text.toLowerCase() === label);
  if (!button?.key) throw new Error(`Missing ${label} button`);
  await ui.press({ key: button.key });
}

describe("unit board commands and lifetime", () => {
  test("startup registers only accounts-board", async ($, on) => {
    mock.env(on, { HOME: "/test-home", USER_ZONE: "UTC" });
    const registered: string[] = [];
    on("session.start", (_$, event) => ({ cwd: event.cwd }));
    on("ui.panes", () => ({ value: [] }));
    on("command.register", (_$, event) => {
      registered.push(event.name);
      return { value: { command: event.name } };
    });

    await $.session.start(start);
    expect(registered).toEqual(["accounts-board"]);
  });

  test("the showrunner command handles open and close without a model turn and leaves the retired command alone", async ($, on) => {
    const fake = engineFiles(on);
    const forwarded: string[] = [];
    on("command.run", (_$, event) => {
      forwarded.push(event.command);
      return { text: "Command reached the model." };
    });

    await $.session.start(start);
    expect(await $.command.run(command())).toEqual({ text: "Unit board opened." });
    expect(fake.opened).toEqual(["unit-board"]);
    expect(await $.command.run(command("close"))).toEqual({ text: "Unit board closed." });
    expect(fake.closed).toEqual(["unit-board"]);
    expect(forwarded).toEqual([]);

    fake.clear();
    expect(await $.command.run({ ...command(), command: "unit-board" }))
      .toEqual({ text: "Command reached the model." });
    expect(forwarded).toEqual(["unit-board"]);
    expect(fake.reads).toEqual([]);
    expect(fake.stats).toEqual([]);
    expect(fake.processes).toEqual([]);
    expect(fake.opened).toEqual(["unit-board"]);
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
      [{ kind: "board", board: `/${pane}.json`, sources: [`/${pane}-source`] }],
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

  for (const surface of ["terminal", "desktop"] as const) {
    test(`${surface}: command opens board and buttons navigate to the unit and back`, async ($, on) => {
      const fake = engineFiles(on);
      await $.session.start(start);
      const result = await $.command.run(command());
      expect(result.text).toBe("Unit board opened.");
      expect(fake.processes).toEqual([["/test-home/.claude/scripts/lib/py",
        "/test-home/.claude/scripts/direct/board_json.py", "--session", SESSION]]);
      expect(fake.opened).toEqual(["unit-board"]);
      const ui = await mount($, surface);
      let text = drawnText(await ui.drawn());
      expect(text).toContain("show");
      expect(text).toMatch(/as of\s+14:00/);
      expect(text).toContain("alpha");
      expect(text).toContain("run ends 14:42");
      expect(text).toContain("ETA 14:30 · in 30m");
      expect(text).not.toContain("first 17:15");
      expect(text).not.toContain("moved 2×");
      expect(text).not.toContain("+1:15 late");
      expect(text).not.toContain("│");
      expect(text).toContain("1 + 1 running");
      await press(ui, "alpha");
      text = drawnText(await ui.drawn());
      expect(text).toContain("Finishing the retry path");
      expect(text).toContain("first 17:15");
      expect(text).toContain("moved 2×");
      expect(text).toContain("+1:15 late");
      expect(text).toContain("17:15 said 16:30");
      expect(text).toContain("Check retry path");
      expect(text).toContain("Fixing the retry case");
      expect(text).toContain("early");
      expect(text).toContain("17:30");
      await press(ui, "board");
      expect(drawnText(await ui.drawn())).toContain("show");
      expect(fake.processes.length).toBe(1);
    });

    test(`${surface}: explicit unit selects the first production holding that key`, async ($, on) => {
      const fake = engineFiles(on);
      const first = fixture();
      first.units[0]!.key = "beta";
      first.units[0]!.summary = "Unmatched production delivery";
      const second = fixture();
      second.production = "second";
      second.units[0]!.summary = "First matching production delivery";
      const third = fixture();
      third.production = "third";
      third.units[0]!.summary = "Later matching production delivery";
      fake.replaceBoards([first, second, third]);
      await $.session.start(start);
      await $.command.run(command("alpha"));
      const ui = await mount($, surface);
      const text = drawnText(await ui.drawn());
      expect(text).toContain("First matching production delivery");
      expect(text).not.toContain("Unmatched production delivery");
      expect(text).not.toContain("Later matching production delivery");
      await press(ui, "board");
      const board = drawnText(await ui.drawn());
      expect(board).toContain("show");
      expect(board).toContain("second");
      expect(board).toContain("third");
    });

    test(`${surface}: an unknown unit argument opens the board view`, async ($, on) => {
      const fake = engineFiles(on);
      const second = fixture();
      second.production = "second";
      fake.replaceBoards([fixture(), second]);
      await $.command.run(command("missing-unit"));
      const ui = await mount($, surface);
      const text = drawnText(await ui.drawn());
      expect(text).toContain("show");
      expect(text).toContain("second");
      expect(text).not.toContain("Finishing the retry path");
      expect((await ui.findAll({ type: "Button" })).map(button => button.text)).toEqual(["alpha", "alpha"]);
    });
  }

  test("closed startup and turn completion perform no file, process, or timer work", async ($, on) => {
    const fake = engineFiles(on);
    await $.session.start(start);
    expect(await $.turn.complete(turn)).toEqual({ text: turn.answer });
    await fake.clock.advance(60_000);
    expect(fake.opened).toEqual([]);
    expect(fake.reads).toEqual([]);
    expect(fake.stats).toEqual([]);
    expect(fake.processes).toEqual([]);
    expect(fake.forbidden).toEqual([]);
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

  test("command close keeps a source change quiet over the following minute", async ($, on) => {
    const fake = engineFiles(on);
    await $.session.start(start);
    await $.command.run(command());
    await $.command.run(command("close"));
    expect(fake.closed).toEqual(["unit-board"]);
    fake.clear();
    fake.mtimes.set(DISCOVERY, NOW + 1);
    await $.turn.complete(turn);
    await fake.clock.advance(60_000);
    expect(fake.reads).toEqual([]);
    expect(fake.stats).toEqual([]);
    expect(fake.processes).toEqual([]);
  });

  test("opening the board passes only the current session to the generator", async ($, on) => {
    const fake = engineFiles(on);
    fake.setSession("command-showrunner-id");

    await $.command.run(command());

    expect(fake.processes.length).toBe(1);
    const argv = fake.processes[0]!;
    expect(argv).not.toContain("--cwd");
    expect(argv).toContain("--session");
    expect(argv[argv.indexOf("--session") + 1]).toBe("command-showrunner-id");
  });

  test("session start reads the resumed session id and retains it for changed-source refreshes", async ($, on) => {
    const fake = engineFiles(on);
    await $.command.run(command());
    fake.setSession("resumed-showrunner-id");
    fake.clear();

    await $.session.start(start);
    fake.clear();
    fake.setSession("later-service-id");
    fake.mtimes.set(RUN, NOW + 1);
    await fake.clock.advance(10_000);

    expect(fake.processes.length).toBe(1);
    const refreshed = fake.processes[0]!;
    expect(refreshed).not.toContain("--cwd");
    expect(refreshed).toContain("--session");
    expect(refreshed[refreshed.indexOf("--session") + 1]).toBe("resumed-showrunner-id");
    expect(fake.reads).toEqual([BOARD]);
  });

  test("session restart with an existing pane resumes one refresh loop", async ($, on) => {
    const fake = engineFiles(on);
    await $.session.start(start);
    await $.command.run(command());
    fake.clear();
    await $.session.start(start);
    expect(fake.opened).toEqual(["unit-board"]);
    fake.clear();
    await fake.clock.advance(10_000);
    expect(fake.stats.filter(path => path === BOARD).length).toBe(1);
    expect(fake.stats.filter(path => path === DISCOVERY).length).toBe(1);
    expect(fake.processes).toEqual([]);
    expect(fake.reads).toEqual([]);
  });

  for (const path of [RUN, DISCOVERY]) {
    test(`a changed ${path} regenerates once and redraws the board`, async ($, on) => {
      const fake = engineFiles(on);
      await $.session.start(start);
      await $.command.run(command());
      const ui = await mount($, "terminal");
      const changed = fixture();
      changed.units[0]!.stage = "Tests ready";
      fake.replace(changed);
      fake.mtimes.set(path, NOW + 1);
      fake.clear();
      await fake.clock.advance(10_000);
      expect(fake.processes.length).toBe(1);
      expect(fake.reads).toEqual([BOARD]);
      expect(drawnText(await ui.drawn())).toContain("Tests ready");
      await $.turn.complete(turn);
      expect(fake.processes.length).toBe(1);
    });
  }

  test("unchanged ticks move now past the promise while keeping the snapshot and promise", async ($, on) => {
    const fake = engineFiles(on);
    await fake.clock.advance(29 * 60_000);
    await $.session.start(start);
    await $.command.run(command());
    const ui = await mount($, "terminal");
    const before = drawnText(await ui.drawn());
    fake.clear();
    await fake.clock.advance(2 * 60_000);
    const after = drawnText(await ui.drawn());
    expect(after).not.toBe(before);
    expect(before).toContain("ETA 14:30 · in 1m");
    expect(after).toContain("ETA 14:30 · late 1m");
    expect(after).toMatch(/1 late/);
    expect(after).toMatch(/as of\s+14:00/);
    expect(after).not.toContain("│");
    expect(fake.processes).toEqual([]);
    expect(fake.reads).toEqual([]);
    expect(fake.stats).toContain(RUN);
  });

  test("missing board and disappeared source each trigger one rebuild", async ($, on) => {
    const fake = engineFiles(on);
    await $.session.start(start);
    await $.command.run(command());
    fake.clear();
    fake.mtimes.delete(BOARD);
    await $.turn.complete(turn);
    expect(fake.processes.length).toBe(1);
    fake.mtimes.delete(RUN);
    await $.turn.complete(turn);
    expect(fake.processes.length).toBe(2);
  });

  for (const kind of ["plugin", "person", "unload"] as const) {
    test(`${kind} close drops pane state and cancels all future refresh work`, async () => {
      // The engine kit cannot raise ui.close. Exercise the registered lifecycle
      // hooks directly for the host-only person and unload origins.
      const pane = lifecycleHarness();
      await pane.dispatch("command.run", command());
      expect(pane.panes()["unit-board"]?.kind).toBe("board");
      pane.clearCalls();
      await pane.dispatch("ui.close", { id: "unit-board", origin: { kind } });
      expect(pane.panes()["unit-board"]).toBeUndefined();
      expect(pane.cancelledTimers()).toBe(1);
      expect(pane.forwardedCloses()).toBe(1);
      pane.clearCalls();
      await pane.dispatch("turn.complete", turn);
      await pane.advanceMinute();
      expect(pane.calls).toEqual([]);
    });
  }

  test("timed, untimed and cleared gates and all finish outcomes use the stored fields", async ($, on) => {
    const fake = engineFiles(on);
    await $.session.start(start);
    for (const kind of ["timed", "untimed", "none"] as const) {
      const data = fixture();
      const row = data.units[0]!.phases[2]!;
      row.gate = kind === "none" ? { kind } : kind === "timed"
        ? { kind, gate: "G3", until: time(120, "Wed 22 Oct 11:47"), waits_on: "renderer review" }
        : { kind, gate: "G3", waits_on: "renderer review" };
      row.status = kind === "none" ? "todo" : "gated";
      fake.replace(data);
      await $.command.run(command());
      const ui = await mount($, "desktop");
      const text = drawnText(await ui.drawn());
      if (kind === "none") expect(text).not.toContain("waits on G3");
      else {
        expect(text).toContain(kind === "timed"
          ? "Phase 3 waits on G3 until 16:00: renderer review"
          : "Phase 3 waits on G3: renderer review");
        expect(text).not.toContain("Wed 22 Oct 11:47");
      }
      await $.command.run(command("close"));
      await ui.unmount();
    }
    for (const kind of ["predicted", "finished", "after_gate", "unknown"] as const) {
      const data = fixture();
      data.units[0]!.finish = kind === "unknown" ? { kind } : kind === "after_gate"
        ? { kind, gate: "G3", waits_on: "renderer review" } : { kind, at: time(70, "19:10") };
      fake.replace(data);
      await $.command.run(command());
      const ui = await mount($, "terminal");
      const text = drawnText(await ui.drawn());
      if (kind === "unknown") expect(text).not.toMatch(/run ends|run ended/);
      else expect(text).toContain(kind === "predicted" ? "run ends 15:10"
        : kind === "finished" ? "run ended 15:10" : "run ends after G3");
      await $.command.run(command("close"));
      await ui.unmount();
    }
  });

  for (const surface of ["terminal", "desktop"] as const) {
    test(`${surface}: opening a repeated unit key retains its production through refresh`, async ($, on) => {
      const fake = engineFiles(on);
      const first = fixture();
      first.units[0]!.summary = "First production delivery";
      const second = fixture();
      second.production = "second";
      second.units[0]!.summary = "Second production delivery";
      fake.replaceBoards([first, second]);
      await $.command.run(command());
      const ui = await mount($, surface);
      const buttons = await ui.findAll({ type: "Button" });
      expect(buttons.length).toBe(2);
      await ui.press({ key: buttons[1]!.key! });
      expect(drawnText(await ui.drawn())).toContain("Second production delivery");
      expect(drawnText(await ui.drawn())).not.toContain("First production delivery");
      second.units[0]!.summary = "Second production refreshed";
      fake.mtimes.set(RUN, NOW + 1);
      await fake.clock.advance(10_000);
      expect(drawnText(await ui.drawn())).toContain("Second production refreshed");
      await press(ui, "board");
      expect(drawnText(await ui.drawn())).toContain("second");
    });
  }

  for (const removed of ["production", "unit"] as const) {
    test(`a selected ${removed} disappearing returns to the board without selecting a namesake`, async ($, on) => {
      const fake = engineFiles(on);
      const second = fixture();
      second.production = "second";
      second.units[0]!.summary = "Second namesake";
      fake.replaceBoards([fixture(), second]);
      await $.command.run(command("alpha"));
      const ui = await mount($, "terminal");
      expect(drawnText(await ui.drawn())).toContain("Finishing the retry path");
      if (removed === "production") fake.replaceBoards([second]);
      else {
        const first = fixture();
        first.units = [];
        fake.replaceBoards([first, second]);
      }
      fake.mtimes.set(REGISTRY, NOW + 1);
      await fake.clock.advance(10_000);
      const text = drawnText(await ui.drawn());
      expect(text).toContain("second");
      expect(text).not.toContain("Second namesake");
      const buttons = await ui.findAll({ type: "Button" });
      expect(buttons.some(button => button.text === "alpha")).toBe(true);
    });
  }

  test("a failed refresh retains its selected production and recovers after another source change", async ($, on) => {
    const fake = engineFiles(on);
    await $.command.run(command("alpha"));
    const ui = await mount($, "terminal");
    fake.failNext("Temporary generator outage");
    fake.mtimes.set(RUN, NOW + 1);
    await fake.clock.advance(10_000);
    expect(drawnText(await ui.drawn())).toContain("Temporary generator outage");
    const restored = fixture();
    restored.units[0]!.summary = "Recovered unit delivery";
    fake.replace(restored);
    fake.mtimes.set(RUN, NOW + 10_001);
    await fake.clock.advance(10_000);
    expect(drawnText(await ui.drawn())).toContain("Recovered unit delivery");
    expect(fake.processes.length).toBe(3);
  });

  test("a failed first load watches the registry and recovers without reopening", async ($, on) => {
    const fake = engineFiles(on);
    fake.failNext("Registry is temporarily unavailable");
    await $.command.run(command());
    const ui = await mount($, "terminal");
    expect(drawnText(await ui.drawn())).toContain("Registry is temporarily unavailable");
    fake.clear();
    await fake.clock.advance(10_000);
    expect(fake.processes).toEqual([]);
    expect(fake.reads).toEqual([]);
    fake.mtimes.set(REGISTRY, NOW + 10_001);
    await fake.clock.advance(10_000);
    expect(fake.processes.length).toBe(1);
    expect(drawnText(await ui.drawn())).toContain("show");
    expect(fake.opened).toEqual(["unit-board"]);
  });

  for (const during of ["initial load", "refresh"] as const) {
    test(`closing during a pending ${during} prevents all later file reads and state writes`, async () => {
      const pane = lifecycleHarness();
      if (during === "refresh") await pane.dispatch("command.run", command());
      const generator = pane.pauseGenerator();
      pane.changeSource();
      const loading = pane.dispatch(during === "refresh" ? "turn.complete" : "command.run",
        during === "refresh" ? turn : command());
      await generator.started;
      await pane.dispatch("ui.close", { id: "unit-board", origin: { kind: "person" } });
      pane.clearCalls();
      generator.finish();
      await loading;
      expect(pane.calls).toEqual([]);
      expect(pane.panes()["unit-board"]).toBeUndefined();
      await pane.advanceMinute();
      expect(pane.calls).toEqual([]);
    });
  }

  test("an empty production list stays quiet until registry discovery changes", async ($, on) => {
    const fake = engineFiles(on);
    fake.replaceBoards([]);
    await $.command.run(command());
    const ui = await mount($, "terminal");
    expect(drawnText(await ui.drawn())).toContain("This session is not on the staff list.");
    fake.clear();
    await fake.clock.advance(10_000);
    expect(fake.processes).toEqual([]);
    expect(fake.reads).toEqual([]);
    expect(fake.stats).toContain(REGISTRY);
    fake.replace(fixture());
    fake.mtimes.set(REGISTRY, NOW + 10_001);
    await fake.clock.advance(10_000);
    expect(fake.processes.length).toBe(1);
    expect(drawnText(await ui.drawn())).toContain("show");
    await $.turn.complete(turn);
    expect(fake.processes.length).toBe(1);
  });

  test("board workers are counted by state while the unit retains roles and individual rows", async ($, on) => {
    const fake = engineFiles(on);
    const data = fixture();
    const worker = data.units[0]!.workers[0]!;
    data.units[0]!.workers = [worker,
      { ...worker, slot: "2", role: "review", state: "idle", doing: "Review worker two" },
      { ...worker, slot: "3", role: "review", state: "idle", doing: "Review worker three" },
      { ...worker, slot: "4", role: "fix", state: "idle", doing: "Idle repair worker" }];
    fake.replace(data);
    await $.command.run(command());
    const ui = await mount($, "terminal");
    const board = drawnText(await ui.drawn()).replace(/\s+/g, " ");
    expect(board).toContain("workers 1 running · 3 idle");
    expect(board).not.toContain("review");
    expect(board).not.toContain("fix running");
    await press(ui, "alpha");
    const text = drawnText(await ui.drawn());
    expect(text).toContain("Review worker two");
    expect(text).toContain("Review worker three");
    expect(text).toContain("Idle repair worker");
    expect(text).toContain("review");
    expect(text).toContain("fix");
  });

});


describe("accounts pane clock", () => {
  const accountsCommand: CommandRunInput = { ...command(), command: "accounts-board" };

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
      const state = event.value["accounts-board"];
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
    test(`${surface}: an unchanged tick across local midnight redraws resets and read age from the new now`, async ($, on) => {
      const fake = accountsClockHost(on);
      const midnight = Date.UTC(2026, 9, 17, 4, 0);
      await fake.clock.advance(midnight - NOW - 5_000);
      await $.command.run(accountsCommand);
      const ui = await $.ui.mount({ plugin: "unit-board", requestId: "accounts-board", component: "Pane", surface,
        props: { ...props, title: "Accounts", bodyColumns: 120 } });
      const before = drawnText(await ui.drawn()).replace(/\s+/g, " ");
      expect(before).toContain("account role left resets credits enabled in use read");
      expect(before).toMatch(/● Fixture workers 60% 23:59 0s\s*$/);
      await fake.clock.advance(10_000);
      const after = drawnText(await ui.drawn()).replace(/\s+/g, " ");
      expect(after).toContain("account role left resets credits enabled in use read");
      expect(after).toMatch(/● Fixture workers 60% Fri 23:59 10s\s*$/);
      expect(fake.snapshots.at(-1)?.now).toBe((midnight + 5_000) / 1000);
      expect(fake.processes.length).toBe(1);
      expect(fake.reads).toEqual([]);
    });
  }
});
