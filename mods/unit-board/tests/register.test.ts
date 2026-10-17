import { describe, expect, mock, test } from "claude-code/testing";
import type { Engine, Mounted } from "claude-code/testing";
import type { CommandRunInput, RenderSurface, TurnCompleteInput } from "claude-code";
import { refreshIfChanged } from "../hooks/data";
import { BOARD, CWD, DISCOVERY, drawnText, engineFiles, fixture, hostEngine, lifecycleHarness, NOW, REGISTRY, RUN, time } from "./kit";

const command = (args = ""): CommandRunInput => ({ command: "unit-board", args,
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
        "/test-home/.claude/scripts/delegate/board_json.py", "--cwd", CWD]]);
      expect(fake.opened).toEqual(["unit-board"]);
      const ui = await mount($, surface);
      let text = drawnText(await ui.drawn());
      expect(text).toContain("show");
      expect(text).toMatch(/as of\s+18:00/);
      expect(text).toContain("alpha");
      expect(text).toContain("18:42");
      expect(text).toContain("first 17:15");
      expect(text).toContain("moved 2×");
      expect(text).toContain("+1:15 late");
      expect(text).toContain("1 + 1 running");
      await press(ui, "open");
      text = drawnText(await ui.drawn());
      expect(text).toContain("Finishing the retry path");
      expect(text).toContain("17:15 said 16:30");
      expect(text).toContain("Check retry path");
      expect(text).toContain("Fixing the retry case");
      expect(text).toContain("early");
      expect(text).toContain("17:30");
      await press(ui, "board");
      expect(drawnText(await ui.drawn())).toContain("show");
      expect(fake.processes.length).toBe(1);
    });

    test(`${surface}: generator focus and explicit unit both open the unit view`, async ($, on) => {
      const fake = engineFiles(on, "alpha");
      await $.session.start(start);
      await $.command.run(command());
      const ui = await mount($, surface);
      expect(drawnText(await ui.drawn())).toContain("Finishing the retry path");
      await $.command.run(command("close"));
      await $.command.run(command("alpha"));
      expect(drawnText(await ui.drawn())).toContain("Finishing the retry path");
      expect(fake.processes.length).toBe(2);
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

  test("explicit unit selects the detail view even without generator focus", async ($, on) => {
    engineFiles(on);
    await $.session.start(start);
    await $.command.run(command("alpha"));
    const ui = await mount($, "terminal");
    expect(drawnText(await ui.drawn())).toContain("Finishing the retry path");
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
    expect(after).toContain("14:31");
    expect(after).toContain("18:30");
    expect(after).toMatch(/as of\s+18:00/);
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
      if (kind === "none") expect(text).not.toContain("gated");
      else {
        expect(text).toContain(kind === "timed" ? "gated until Wed 22 Oct 11:47" : "gated by G3");
        expect(text).toContain("renderer review");
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
      if (kind === "unknown") expect(text).not.toContain("done by");
      else expect(text).toContain(kind === "predicted" ? "done by 19:10"
        : kind === "finished" ? "finished 19:10" : "after G3");
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
      expect(buttons.some(button => button.text === "open")).toBe(true);
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
    expect(drawnText(await ui.drawn())).toContain("No running productions");
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

  test("board workers are counted by role and state while the unit retains individual rows", async ($, on) => {
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
    expect(drawnText(await ui.drawn())).toContain("1 fix running · 2 review idle · 1 fix idle");
    await press(ui, "open");
    const text = drawnText(await ui.drawn());
    expect(text).toContain("Review worker two");
    expect(text).toContain("Review worker three");
    expect(text).toContain("Idle repair worker");
  });

});
