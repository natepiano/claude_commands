import { describe, expect, test } from "claude-code/testing";
import type { Engine } from "claude-code/testing";
import type { On, RenderSurface } from "claude-code";
import { BoardView } from "../hooks/board";
import { UnitView } from "../hooks/unit";
import { cellWidth, fitText } from "../hooks/geometry";
import type { BoardData, BoardUnit } from "../types/board";
import { engineFiles, fixture, NOW, phase, time } from "./kit";

const props = { title: "Units", isFocused: true, bodyColumns: 170,
  scroll: { offset: 0, bodyRows: 50 }, view: {}, placement: "dock" as const };

function prepareRender($: Engine, on: On, surface: RenderSurface, data: BoardData, detail = false, columns = 170, bodyRows = 50, requestId = "phase17-layout") {
  on("ui.render", { component: "Pane", requestId }, ($, event) => {
    const elements = $.ui.resolve(event);
    return detail
      ? UnitView(elements, columns, bodyRows, data, data.units[0]!, NOW / 1000, () => {})
      : BoardView(elements, columns, bodyRows, { files: [], watch: [], loadedAt: NOW / 1000,
        boards: [data] }, NOW / 1000, () => {});
  });
  return () => $.ui.mount({ plugin: "unit-board", component: "Pane", requestId, surface,
    props: { ...props, bodyColumns: columns, scroll: { offset: 0, bodyRows } } });
}

async function render($: Engine, on: On, surface: RenderSurface, data: BoardData, detail = false, columns = 170, bodyRows = 50) {
  return prepareRender($, on, surface, data, detail, columns, bodyRows)();
}

function node(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null ? value as Record<string, unknown> : {};
}
function children(value: unknown): unknown[] {
  if (Array.isArray(value)) return value;
  const found = node(value).children;
  return Array.isArray(found) ? found : found === undefined ? [] : [found];
}
/** Preserve adjacent text spans and layout rows, unlike drawnText's word joining. */
function lines(value: unknown): string[] {
  if (value === null || value === undefined || typeof value === "boolean") return [];
  if (typeof value === "string" || typeof value === "number") return [String(value)];
  if (Array.isArray(value)) return value.flatMap(lines);
  const element = node(value);
  const attributes = node(element.props);
  const parts = children(value).flatMap(lines);
  if (element.type === "Text") return [parts.join("")];
  if (element.type === "Button") return [String(attributes.label ?? element.label ?? parts.join(""))];
  if (element.type !== "Box") return parts;
  const content = attributes.flexDirection === "column" ? parts
    : parts.length ? [parts.join(" ".repeat(Number(attributes.columnGap ?? attributes.gap ?? 0)))] : [];
  const indent = " ".repeat(Number(attributes.paddingLeft ?? attributes.paddingX ?? attributes.padding ?? 0));
  return content.map(line => indent + line);
}
function emptyBoxes(value: unknown): number {
  return (node(value).type === "Box" && lines(value).every(line => !line.trim()) ? 1 : 0)
    + children(value).reduce<number>((count, child) => count + emptyBoxes(child), 0);
}
function unit(key: string, state: BoardUnit["state"]): BoardUnit {
  return { ...fixture().units[0]!, key, state };
}
function noPhase(): BoardData {
  const data = fixture();
  Object.assign(data.units[0]!, { phase: null, phases: [], eta: null, stage: "Standing by",
    finish: { kind: "unknown" }, workers: [], steps: [], summary: "" });
  return data;
}

/** Include reserved blank rows so a tree that overflows the pane cannot pass. */
function occupiedRows(value: unknown): number {
  if (!lines(value).length) return 0;
  if (Array.isArray(value)) return value.reduce((total, child) => total + occupiedRows(child), 0);
  const element = node(value);
  if (element.type !== "Box") return lines(value).reduce((total, line) => total + line.split(/\r\n|\r|\n/).length, 0);
  const attributes = node(element.props);
  const heights = children(value).map(occupiedRows).filter(height => height > 0);
  const body = attributes.flexDirection === "column"
    ? heights.reduce((total, height) => total + height, 0)
      + Math.max(0, heights.length - 1) * Number(attributes.rowGap ?? attributes.gap ?? 0)
    : Math.max(0, ...heights);
  return body + Number(attributes.marginTop ?? 0) + Number(attributes.marginBottom ?? 0)
    + Number(attributes.paddingTop ?? attributes.paddingY ?? attributes.padding ?? 0)
    + Number(attributes.paddingBottom ?? attributes.paddingY ?? attributes.padding ?? 0);
}

/** Preserve vertical spacing when checking the rows a person sees. */
function layoutRows(value: unknown): string[] {
  if (Array.isArray(value)) return value.flatMap(layoutRows);
  if (node(value).type !== "Box") return lines(value);
  const attributes = node(node(value).props);
  const content = attributes.flexDirection === "column"
    ? children(value).map(layoutRows).filter(rows => rows.length).flatMap((rows, index) =>
      [...Array<string>(index ? Number(attributes.rowGap ?? attributes.gap ?? 0) : 0).fill(""), ...rows])
    : lines(value);
  const top = Number(attributes.marginTop ?? 0)
    + Number(attributes.paddingTop ?? attributes.paddingY ?? attributes.padding ?? 0);
  const bottom = Number(attributes.marginBottom ?? 0)
    + Number(attributes.paddingBottom ?? attributes.paddingY ?? attributes.padding ?? 0);
  return [...Array<string>(top).fill(""), ...content, ...Array<string>(bottom).fill("")];
}

function longUnit(): BoardData {
  const data = fixture();
  const active = data.units[0]!;
  active.phase = { kind: "plan", id: "10", of: 17, title: "Current delivery" };
  active.phases = Array.from({ length: 17 }, (_, index) => {
    const id = index + 1;
    const row = phase(String(id), id < 10 ? "done" : id === 10 ? "open" : "todo");
    row.title = `Record ${id}`;
    return row;
  });
  active.phases[9]!.start = time(-60, "17:00");
  active.steps = Array.from({ length: 8 }, (_, index) => [
    index === 7 ? "running" : "completed", `Step ${index + 1}`,
    time(-56 + index * 7, `17:${String(4 + index * 7).padStart(2, "0")}`), 420,
  ]);
  active.workers = Array.from({ length: 5 }, (_, index) => ({
    slot: `worker-${index + 1}`, role: "test", state: "running", model: "", doing: `Check ${index + 1}`,
  }));
  return data;
}

/** Inspect inherited colors, including facts nested inside a colored Text. */
function coloredText(value: unknown, inherited = "default"): { text: string; color: string }[] {
  if (typeof value === "string" || typeof value === "number") return [{ text: String(value), color: inherited }];
  if (value === null || value === undefined || typeof value === "boolean") return [];
  if (Array.isArray(value)) return value.flatMap(child => coloredText(child, inherited));
  const attributes = node(node(value).props);
  const color = typeof attributes.color === "string" ? attributes.color : inherited;
  return children(value).flatMap(child => coloredText(child, color));
}
function elements(value: unknown): Record<string, unknown>[] {
  if (Array.isArray(value)) return value.flatMap(elements);
  if (typeof value !== "object" || value === null) return [];
  return [node(value), ...children(value).flatMap(elements)];
}
function textInColor(value: unknown, color: string): string {
  return coloredText(value).filter(span => span.color === color).map(span => span.text).join("");
}
function normalized(rows: string[]): string[] {
  return rows.filter(row => row.trim()).map(row => row.trim().replace(/\s+/g, " "));
}
function quietUnit(key: string, state: BoardUnit["state"]): BoardUnit {
  return { ...noPhase().units[0]!, key, state,
    stage: state === "run-finished" ? "Run done" : "Standing by" };
}
function activeUnits(): BoardData {
  const data = fixture();
  data.units = ["a", "b", "c"].map(key => {
    const active = unit(key, "running");
    active.stage = "Build";
    active.eta!.fix_done = 0;
    active.eta!.fix_running = 0;
    active.finish = { kind: "unknown" };
    return active;
  });
  return data;
}
function gated(data: BoardData, timed = false): void {
  const row = data.units[0]!.phases[2]!;
  row.status = "gated";
  row.gate = timed
    ? { kind: "timed", gate: "G1", until: time(60, "wrong gate time"), waits_on: "design approval" }
    : { kind: "untimed", gate: "G1", waits_on: "design approval" };
}

// Both element surfaces must carry the same facts, colors and height budget.
describe("phase 17 board", () => {
  for (const surface of ["terminal", "desktop"] as const) {
    test(`${surface}: all states share a case-insensitive alphabetical list with clickable bold names`, async ($, on) => {
      const data = fixture();
      data.units = [quietUnit("zulu", "run-finished"), unit("delta", "running"),
        quietUnit("Bravo", "standing-by"), quietUnit("alpha", "run-finished")];
      const ui = await render($, on, surface, data);
      const tree = await ui.drawn();
      const rows = lines(tree);
      expect(rows.filter(row => row.includes("●")).map(row => row.match(/●\s+(\S+)/)?.[1]))
        .toEqual(["alpha", "Bravo", "delta", "zulu"]);
      expect(rows.join("\n")).not.toMatch(/(?:^|\n)(?:Done:|Standing by:)/);
      expect(await ui.findAll({ type: "Button", text: /^open$/i })).toEqual([]);
      for (const key of ["alpha", "Bravo", "delta", "zulu"]) {
        const buttons = await ui.findAll({ type: "Button", text: new RegExp(`^${key}$`) });
        expect(buttons).toHaveLength(1);
        expect(buttons[0]!.props.plain).toBe(true);
        const bold = elements(tree).some(element => node(element.props).bold === true
          && lines(element).join("").includes(key));
        expect(bold).toBe(true);
      }
    });

    test(`${surface}: pressing the unit name opens that unit's detail`, async ($, on) => {
      const fake = engineFiles(on);
      const data = fixture();
      data.units[0]!.summary = "Named unit detail reached";
      fake.replace(data);
      await $.command.run({ command: "showrunner:unit_board", args: "",
        origin: { kind: "composer" }, presentation: { isFullscreen: false, columns: 170 } });
      const ui = await $.ui.mount({ plugin: "unit-board", component: "Pane", requestId: "unit-board",
        surface, props });
      const button = (await ui.findAll({ type: "Button", text: /^alpha$/ }))[0];
      if (!button?.key) throw new Error("Missing named unit button");
      await ui.press({ key: button.key });
      expect(lines(await ui.drawn()).join("\n")).toContain("Named unit detail reached");
      expect(await ui.findAll({ type: "Button", text: /^board$/ })).toHaveLength(1);
      expect(fake.processes).toHaveLength(1);
    });

    test(`${surface}: an active unit uses three rows without a ruler or ETA history`, async ($, on) => {
      const data = fixture();
      data.units[0]!.stage = "Phase 2 running";
      const ui = await render($, on, surface, data);
      const tree = await ui.drawn();
      const rows = normalized(lines(tree));
      const start = rows.findIndex(row => row.includes("●"));
      expect(rows.slice(start)).toHaveLength(3);
      expect(rows[start]).toBe("● alpha Phase 2 Delivery 2");
      expect(rows[start + 1]).toMatch(/█▓░ 1\/3.*fix rounds 1 \+ 1 running.*ETA 14:30 · in 30m/);
      expect(rows[start + 2]).toMatch(/workers 1 running.*run ends 14:42/);
      expect(rows.join("\n")).not.toMatch(/Phase 2 running|first ETA|moved|│|━|─/);
      expect(occupiedRows(tree)).toBeLessThanOrEqual(5);
    });

    for (const merged of [0, 62]) {
      test(`${surface}: ticket ${merged} merged keeps the title and drops the redundant running stage`, async ($, on) => {
        const data = fixture();
        const active = data.units[0]!;
        active.phase = { kind: "ticket", id: "alpha-63", merged, title: "Fix names" };
        active.stage = "Ticket alpha-63 running";
        const ui = await render($, on, surface, data);
        const rows = normalized(lines(await ui.drawn()));
        expect(rows.find(row => row.includes("●"))).toBe(`● alpha Ticket alpha-63 · ${merged} merged Fix names`);
        expect(rows.join("\n")).not.toContain(active.stage);
      });
    }

    test(`${surface}: a grown 132-phase plan draws twenty bar cells and the actual done total`, async ($, on) => {
      const data = fixture();
      const active = data.units[0]!;
      active.phase = { kind: "plan", id: "75", of: 74, title: "New delivery" };
      active.phases = Array.from({ length: 132 }, (_, index) =>
        phase(String(index + 1), index < 74 ? "done" : index === 74 ? "open" : "todo"));
      const ui = await render($, on, surface, data);
      const rows = lines(await ui.drawn());
      const strip = rows.find(row => row.includes("74/132"))!;
      expect((strip.match(/[█▓░▒]/g) ?? [])).toHaveLength(20);
      expect(rows.find(row => row.includes("alpha"))).toContain("Phase 75");
      expect(rows.find(row => row.includes("alpha"))).not.toContain("75/74");
      expect(rows.join("\n")).not.toContain("74/132 done");
    });

    test(`${surface}: late ETA and missing ETA use their semantic tones`, async ($, on) => {
      const data = fixture();
      const active = data.units[0]!;
      active.eta!.at = time(-149, "wrong ETA");
      const missing = unit("beta", "running");
      missing.eta = null;
      const noPromise = unit("gamma", "running");
      noPromise.eta!.at = null;
      data.units.push(missing, noPromise);
      const ui = await render($, on, surface, data);
      const tree = await ui.drawn();
      expect(textInColor(tree, "error")).toContain("ETA 11:31 · late 2h 29m");
      expect(textInColor(tree, "warning").match(/no ETA/g)).toHaveLength(2);
      expect(lines(tree).join("\n")).not.toMatch(/wrong ETA|overdue|moved|first ETA/);
    });

    test(`${surface}: workers aggregate by state, in running idle paused order, with meaning in color`, async ($, on) => {
      const data = fixture();
      data.units[0]!.workers = ["paused", "idle", "running", "idle", "paused", "running", "running", "queued"]
        .map((state, index) => ({ slot: `secret-slot-${index}`, role: `secret-role-${index}`, state, model: "", doing: "" }));
      const ui = await render($, on, surface, data);
      const tree = await ui.drawn();
      const workerRow = normalized(lines(tree)).find(row => row.startsWith("workers"))!;
      expect(workerRow).toContain("workers 3 running · 2 idle · 2 paused · 1 queued");
      expect(textInColor(tree, "suggestion")).toContain("3 running");
      expect(textInColor(tree, "warning")).toContain("2 paused");
      expect(textInColor(tree, "default")).toContain("2 idle");
      expect(textInColor(tree, "default")).toContain("1 queued");
      expect(textInColor(tree, "inactive")).toContain("workers");
      expect(lines(tree).join("\n")).not.toMatch(/secret-slot|secret-role/);
    });

    test(`${surface}: fix rounds show only when positive and warn at three`, async ($, on) => {
      const data = activeUnits();
      data.units[1]!.eta!.fix_done = 2;
      data.units[2]!.eta!.fix_done = 3;
      data.units[2]!.eta!.fix_running = 1;
      const ui = await render($, on, surface, data);
      const tree = await ui.drawn();
      const rows = lines(tree);
      expect(rows.join("\n")).not.toContain("fix rounds 0");
      expect(textInColor(tree, "default")).toContain("fix rounds 2");
      expect(textInColor(tree, "warning")).toContain("fix rounds 3 + 1 running");
    });

    test(`${surface}: a running first repair has no fix count until a repair completes`, async ($, on) => {
      const data = activeUnits();
      data.units = data.units.slice(0, 2);
      data.units[0]!.stage = "Repair round 1";
      data.units[1]!.stage = "Repair round 3";
      data.units[0]!.eta!.fix_running = 1;
      data.units[1]!.eta!.fix_done = 2;
      data.units[1]!.eta!.fix_running = 1;
      const ui = await render($, on, surface, data);
      const rows = normalized(lines(await ui.drawn()));
      const second = rows.findIndex(row => row.startsWith("● b "));
      const firstBlock = rows.slice(1, second).join("\n");
      expect(firstBlock).toContain("Repair round 1");
      expect(firstBlock).not.toContain("fix rounds");
      expect(rows.slice(second).join("\n")).toContain("fix rounds 2 + 1 running");
    });

    test(`${surface}: paired facts reach column 85 and move to their own row at width 40`, async ($, on) => {
      const data = fixture();
      data.units[0]!.workers = ["running", "running", "running", "idle", "idle", "idle", "idle",
        "paused", "paused", "paused", "paused", "paused"]
        .map((state, index) => ({ slot: String(index), role: "test", state, model: "", doing: "" }));
      const drawWide = prepareRender($, on, surface, data, false, 85);
      const drawNarrow = prepareRender($, on, surface, data, false, 40, 50, "phase17-repair-narrow");
      const wide = await drawWide();
      const wideRows = lines(await wide.drawn());
      const eta = wideRows.find(row => row.includes("ETA 14:30"))!;
      const finish = wideRows.find(row => row.includes("run ends 14:42"))!;
      expect(eta).toContain("1/3");
      expect(eta).toMatch(/ {2,}ETA 14:30 · in 30m$/);
      expect(cellWidth(eta)).toBe(85);
      expect(finish).toContain("workers 3 running · 4 idle · 5 paused");
      expect(finish).toMatch(/ {2,}run ends 14:42$/);
      expect(cellWidth(finish)).toBe(85);
      await wide.unmount();
      const narrow = await drawNarrow();
      const narrowRows = lines(await narrow.drawn());
      expect(narrowRows.find(row => row.includes("ETA 14:30"))).toBe("  ETA 14:30 · in 30m");
      expect(narrowRows.find(row => row.includes("run ends 14:42"))).toBe("  run ends 14:42");
      for (const row of narrowRows) expect(cellWidth(row)).toBeLessThanOrEqual(40);
    });

    for (const [kind, expected] of [["predicted", "run ends 14:42"], ["finished", "run ended 14:42"],
      ["after_gate", "run ends after G1"], ["unknown", ""]] as const) {
      test(`${surface}: ${kind} run finish appears on the worker row`, async ($, on) => {
        const data = fixture();
        data.units[0]!.finish = kind === "predicted" || kind === "finished"
          ? { kind, at: time(42, "wrong finish") }
          : kind === "after_gate" ? { kind, gate: "G1", waits_on: "approval" } : { kind };
        const ui = await render($, on, surface, data);
        const tree = await ui.drawn();
        const rows = normalized(lines(tree));
        expect(rows.slice(1)).toHaveLength(3);
        if (expected) expect(rows.find(row => row.startsWith("workers"))).toContain(expected);
        else expect(rows.join("\n")).not.toMatch(/run ends|run ended/);
        expect(rows.join("\n")).not.toContain("wrong finish");
      });
    }

    test(`${surface}: a past predicted run end is red and includes compact lateness`, async ($, on) => {
      const data = fixture();
      data.units[0]!.finish = { kind: "predicted", at: time(-2050, "wrong finish") };
      const ui = await render($, on, surface, data);
      expect(textInColor(await ui.drawn(), "error")).toContain("run ends Thu 03:50 · late 1d 10h");
    });

    test(`${surface}: no-phase units have one row with counts and known finishes`, async ($, on) => {
      const data = fixture();
      const finished = quietUnit("a", "run-finished");
      finished.phases = [phase("1", "done"), phase("2", "done")];
      finished.finish = { kind: "finished", at: time(-10, "wrong finish") };
      const future = quietUnit("b", "standing-by");
      future.phases = [phase("1", "done"), phase("2", "todo")];
      future.finish = { kind: "predicted", at: time(60, "wrong finish") };
      data.units = [finished, future, quietUnit("c", "standing-by")];
      const ui = await render($, on, surface, data);
      const rows = normalized(lines(await ui.drawn()));
      expect(rows.slice(1)).toEqual(["● a Run done · 2/2 · run ended 13:50",
        "● b Standing by · 1/2 · run ends 15:00", "● c Standing by"]);
    });

    for (const timed of [false, true]) {
      test(`${surface}: ${timed ? "timed" : "untimed"} gate is a warning row naming the waiting phase`, async ($, on) => {
        const data = fixture();
        gated(data, timed);
        const ui = await render($, on, surface, data);
        const expected = timed ? "Phase 3 waits on G1 until 15:00: design approval"
          : "Phase 3 waits on G1: design approval";
        expect(textInColor(await ui.drawn(), "warning")).toContain(expected);
      });
    }

    for (const [minutes, expected] of [[30, "14:30"], [1440, "Sat 14:00"], [-1440, "Thu 14:00"],
      [6 * 1440, "Thu 14:00"], [-6 * 1440, "Sat 14:00"], [7 * 1440, "23 Oct"], [-7 * 1440, "9 Oct"]] as const) {
      test(`${surface}: short time ${minutes} minutes from now is ${expected}`, async ($, on) => {
        const data = fixture();
        data.units[0]!.eta!.at = time(minutes, "poisoned time text");
        const ui = await render($, on, surface, data);
        const text = lines(await ui.drawn()).join("\n");
        expect(text).toContain(`ETA ${expected} · ${minutes < 0 ? "late" : "in"}`);
        expect(text).not.toContain("poisoned time text");
      });
    }

    test(`${surface}: local midnight, rather than the UTC date, determines the short-time form`, async ($, on) => {
      const data = fixture();
      data.utc_offset_minutes = 600;
      data.as_of.text = "poisoned as-of text";
      data.units[0]!.eta!.at = time(-300, "poisoned ETA text");
      const ui = await render($, on, surface, data);
      const text = lines(await ui.drawn()).join("\n");
      expect(text).toContain("ETA Fri 23:00 · late 5h 0m");
      expect(text).toContain("as of 04:00");
      expect(text).not.toContain("poisoned");
    });

    test(`${surface}: health dots honor late, gate, no ETA, running, finished and inactive priority`, async ($, on) => {
      const data = fixture();
      const lateEta = unit("a-late-eta", "running");
      lateEta.eta!.at = time(-1, "wrong");
      const lateFinish = unit("b-late-finish", "running");
      lateFinish.finish = { kind: "predicted", at: time(-1, "wrong") };
      const gate = fixture();
      gated(gate);
      gate.units[0]!.key = "c-gate";
      const missing = unit("d-no-eta", "running");
      missing.eta = null;
      data.units = [lateEta, lateFinish, gate.units[0]!, missing, unit("e-running", "running"),
        quietUnit("f-finished", "run-finished"), quietUnit("g-idle", "standing-by")];
      const ui = await render($, on, surface, data);
      const dots = coloredText(await ui.drawn()).filter(span => span.text.includes("●"));
      expect(dots.map(span => span.color)).toEqual(["error", "error", "warning", "warning", "suggestion", "success", "inactive"]);
    });

    test(`${surface}: header counts each late unit once and styles only the late count red`, async ($, on) => {
      const data = fixture();
      data.as_of.text = "wrong header clock";
      const late = unit("a", "running");
      late.eta!.at = time(-1, "wrong");
      late.finish = { kind: "predicted", at: time(-1, "wrong") };
      const lateFinish = unit("b", "running");
      lateFinish.finish = { kind: "predicted", at: time(-1, "wrong") };
      data.units = [late, lateFinish, quietUnit("c", "run-finished")];
      const ui = await render($, on, surface, data);
      const tree = await ui.drawn();
      expect(normalized(lines(tree))[0]).toMatch(/show.*3 units · 2 late · as of 14:00/);
      expect(textInColor(tree, "error")).toContain("2 late");
      expect(textInColor(tree, "inactive")).toContain("3 units");
      expect(textInColor(tree, "inactive")).toContain("as of 14:00");
      expect(elements(tree).some(element => node(element.props).bold === true
        && lines(element).join("") === "show")).toBe(true);
    });

    test(`${surface}: a healthy header omits the zero late count`, async ($, on) => {
      const data = fixture();
      const ui = await render($, on, surface, data);
      expect(normalized(lines(await ui.drawn()))[0]).toMatch(/1 units · as of 14:00/);
      expect(normalized(lines(await ui.drawn()))[0]).not.toContain("late");
    });

    for (const detail of [false, true]) {
      test(`${surface}: ${detail ? "detail" : "board"} uses no subtle or dim text and draws facts in the default color`, async ($, on) => {
        const data = fixture();
        const ui = await render($, on, surface, data, detail);
        const tree = await ui.drawn();
        expect(elements(tree).filter(element => node(element.props).color === "subtle"
          || node(element.props).dimColor === true)).toEqual([]);
        const facts = textInColor(tree, "default");
        if (detail) {
          for (const fact of ["plan.md", "Finishing the retry path", "Build retry path", "17:10", "said 17:00"])
            expect(facts).toContain(fact);
        } else {
          for (const fact of ["Delivery 2", "Repair round 2", "1/3", "ETA 14:30", "run ends 14:42"])
            expect(facts).toContain(fact);
        }
      });
    }

    for (const late of [false, true]) {
      test(`${surface}: detail ruler labels the ${late ? "late" : "future"} promise with its dot's color`, async ($, on) => {
        const data = fixture();
        data.units[0]!.eta!.at = time(late ? -30 : 30, late ? "17:30" : "18:30");
        const ui = await render($, on, surface, data, true);
        const tree = await ui.drawn();
        expect(textInColor(tree, late ? "error" : "success")).toContain(late ? "17:30" : "18:30");
        expect(textInColor(tree, "default")).toContain("│");
      });
    }

    for (const width of [40, 60, 85]) {
      test(`${surface}: long facts wrap without loss or overflow at ${width} cells`, async ($, on) => {
        const data = fixture();
        const active = data.units[0]!;
        const title = "界😀 a very long delivery title with enough words to wrap across the entire pane and retain its final marker TITLE_END";
        const reason = "Wait for the layout design approval and the downstream implementation team to finish their coordinated review GATE_END";
        active.phase!.title = title;
        gated(data, true);
        active.phases[2]!.gate = { kind: "timed", gate: "G1", until: time(60, "wrong"), waits_on: reason };
        active.workers = Array.from({ length: 37 }, (_, index) => ({ slot: `slot-${index}`, role: "review", state: index % 2 ? "idle" : "running", model: "", doing: "" }));
        const ui = await render($, on, surface, data, false, width, 80);
        const tree = await ui.drawn();
        const rows = lines(tree);
        for (const row of rows) {
          expect(row).not.toContain("…");
          expect(cellWidth(row)).toBeLessThanOrEqual(width);
        }
        expect(elements(tree).filter(element => node(element.props).wrap === "truncate-end")).toEqual([]);
        const text = rows.join(" ").replace(/\s+/g, " ");
        expect(text).toContain(title);
        expect(text).toContain(reason);
        expect(text).toContain("ETA 14:30 · in 30m");
        expect(text).toContain("19 running · 18 idle");
        expect(text).toContain("run ends 14:42");
        expect(rows.find(row => row.includes("TITLE_END"))).toMatch(/^ {2}/);
        if (width === 40) {
          const eta = rows.find(row => row.includes("ETA 14:30"))!;
          expect(eta).toMatch(/^ {2}/);
          expect(eta).not.toContain("█");
          const finish = rows.find(row => row.includes("run ends"))!;
          expect(finish).toMatch(/^ {2}/);
          expect(finish).not.toContain("workers");
        }
        expect(occupiedRows(tree)).toBeLessThanOrEqual(80);
      });
    }

    test(`${surface}: the header has a blank row before the first unit until the budget folds gaps`, async ($, on) => {
      const data = activeUnits();
      const drawTall = prepareRender($, on, surface, data, false, 170, 13);
      const drawShort = prepareRender($, on, surface, data, false, 170, 7, "phase17-repair-short");
      const tall = await drawTall();
      const tallRows = layoutRows(await tall.drawn());
      expect(tallRows).toHaveLength(13);
      expect(tallRows[0]).toContain("show");
      expect(tallRows[1]).toBe("");
      expect(tallRows[2]).toMatch(/^● a /);
      expect(tallRows.filter(row => !row.trim())).toHaveLength(3);
      await tall.unmount();
      const short = await drawShort();
      const shortRows = layoutRows(await short.drawn());
      expect(shortRows).toHaveLength(7);
      expect(shortRows[1]).toMatch(/^● a /);
      expect(shortRows.filter(row => !row.trim())).toEqual([]);
      expect(shortRows.filter(row => row.includes("●"))).toHaveLength(3);
    });

    test(`${surface}: height pressure drops worker rows from the bottom before removing gaps or units`, async ($, on) => {
      const data = activeUnits();
      // Three three-row blocks, a gap before each unit, one header = thirteen rows.
      const ui = await render($, on, surface, data, false, 170, 12);
      const tree = await ui.drawn();
      const rows = lines(tree);
      const headings = rows.map((row, index) => row.includes("●") ? index : -1).filter(index => index >= 0);
      expect(headings).toHaveLength(3);
      expect(rows.slice(headings[0], headings[1]).join(" ")).toContain("workers");
      expect(rows.slice(headings[1], headings[2]).join(" ")).toContain("workers");
      expect(rows.slice(headings[2]).join(" ")).not.toContain("workers");
      expect(occupiedRows(tree)).toBe(12);
    });

    test(`${surface}: height pressure removes gaps after every worker row, before whole units`, async ($, on) => {
      const data = activeUnits();
      const ui = await render($, on, surface, data, false, 170, 7);
      const tree = await ui.drawn();
      const rows = lines(tree);
      expect(rows.filter(row => row.includes("●"))).toHaveLength(3);
      expect(rows.join("\n")).not.toContain("workers");
      expect(rows.join("\n")).not.toContain("more:");
      expect(occupiedRows(tree)).toBe(7);
    });

    test(`${surface}: whole bottom units become one alphabetical more line only after gaps are gone`, async ($, on) => {
      const data = activeUnits();
      data.units.push({ ...unit("d", "running"), finish: { kind: "unknown" } });
      data.units.reverse();
      const ui = await render($, on, surface, data, false, 170, 6);
      const tree = await ui.drawn();
      const rows = normalized(lines(tree));
      expect(rows.filter(row => row.includes("●")).map(row => row.match(/●\s+(\S+)/)?.[1])).toEqual(["a", "b"]);
      expect(rows.at(-1)).toBe("+2 more: c, d");
      expect(rows.join("\n")).not.toContain("workers");
      expect(occupiedRows(tree)).toBe(6);
    });

    test(`${surface}: wrapped title rows count before dropping whole units`, async ($, on) => {
      const data = activeUnits();
      data.units.forEach(active => { active.phase!.title = "word ".repeat(24) + "TITLE_END"; });
      const ui = await render($, on, surface, data, false, 40, 10);
      const tree = await ui.drawn();
      const rows = lines(tree);
      expect(occupiedRows(tree)).toBeLessThanOrEqual(10);
      expect(rows.filter(row => row.includes("●"))).toHaveLength(1);
      expect(rows.filter(row => row.includes("TITLE_END"))).toHaveLength(1);
      expect(normalized(rows).at(-1)).toBe("+2 more: b, c");
      for (const row of rows) expect(cellWidth(row)).toBeLessThanOrEqual(40);
    });
  }
});

describe("unit detail layout", () => {
  for (const surface of ["terminal", "desktop"] as const) {
    test(`${surface}: an empty board explains that the session is not on staff`, async ($, on) => {
      on("ui.render", { component: "Pane", requestId: "empty-board" }, ($, event) =>
        BoardView($.ui.resolve(event), 170, 50,
          { files: [], watch: [], loadedAt: NOW / 1000, boards: [] }, NOW / 1000, () => {}));
      const ui = await $.ui.mount({ plugin: "unit-board", component: "Pane", requestId: "empty-board",
        surface, props });

      expect(lines(await ui.drawn()).filter(line => line.trim()))
        .toEqual(["This session is not on the staff list."]);
      expect(await ui.findAll({ type: "Button" })).toEqual([]);
    });

    // Retain the detail heading contracts; only the board drops phase totals.
    for (const merged of [0, 62]) {
      test(`${surface}: detail ticket heading shows ${merged} merged at 76 columns`, async ($, on) => {
        const data = fixture();
        data.units[0]!.phase = { kind: "ticket", id: "alpha-unit-63", title: "Fix names", merged };
        data.units[0]!.stage = "Build";
        const ui = await render($, on, surface, data, true, 76);
        const heading = lines(await ui.drawn()).find(row => row.includes("alpha"))!;
        expect(heading).toContain(`Ticket alpha-unit-63 · ${merged} merged · Build`);
        expect(heading).not.toContain("Phase ");
        expect(cellWidth(heading)).toBeLessThanOrEqual(76);
      });
    }

    test(`${surface}: detail plan heading keeps its phase total at 76 columns`, async ($, on) => {
      const data = fixture();
      data.units[0]!.stage = "Build";
      const ui = await render($, on, surface, data, true, 76);
      const heading = lines(await ui.drawn()).find(row => row.includes("alpha"))!;
      expect(heading).toContain("Phase 2 of 3 · Build");
      expect(heading).not.toContain("Ticket ");
      expect(cellWidth(heading)).toBeLessThanOrEqual(76);
    });

    test(`${surface}: step labels leave two cells before aligned times in a 76-column pane`, async ($, on) => {
      const data = fixture();
      data.units[0]!.steps = [
        ["completed", "Short step", time(-20, "17:40"), 600],
        ["running", "界".repeat(60), time(-10, "17:50"), 600],
      ];
      const ui = await render($, on, surface, data, true, 76, 50);
      const rows = lines(await ui.drawn()).filter(row => /^[✓▶]/.test(row.trimStart()));
      expect(rows).toHaveLength(2);
      expect(rows[0]).toContain("Short step");
      expect(rows[1]).toMatch(/… {2,}17:50 · 0:10$/);
      const offsets = rows.map(row => cellWidth(row.slice(0, row.indexOf("17:"))));
      expect(new Set(offsets).size).toBe(1);
      for (const row of rows) {
        expect(row).toMatch(/ {2,}17:\d{2} · 0:10$/);
        expect(cellWidth(row)).toBeLessThanOrEqual(76);
      }
    });

    test(`${surface}: a 17-phase unit folds history and steps inside 50 rows at 76 columns`, async ($, on) => {
      const ui = await render($, on, surface, longUnit(), true, 76, 50);
      const tree = await ui.drawn();
      const rows = lines(tree);
      expect(occupiedRows(tree)).toBeLessThanOrEqual(50);
      const record = rows.slice(rows.indexOf("Phase record") + 1);
      expect(record.some(row => /\bopen\b.*Record 10$/.test(row))).toBe(true);
      expect(record.some(row => /Record 9$/.test(row))).toBe(true);
      const earlier = record.find(row => /\+\d+ earlier:/.test(row))!;
      expect(earlier).toMatch(/^\+\d+ earlier: 1[–,]/);
      const omitted = Number(earlier.match(/\+(\d+) earlier:/)?.[1]);
      const doneIds = record.filter(row => /Record [1-9]$/.test(row))
        .map(row => Number(row.match(/Record (\d+)$/)?.[1]));
      expect(omitted + doneIds.length).toBe(9);
      expect(doneIds.every(id => id > omitted)).toBe(true);
      expect(record).toContain("+4 later");
      for (const id of [11, 12, 13]) expect(record.some(row => row.endsWith(`Record ${id}`))).toBe(true);
      for (const id of [14, 15, 16, 17]) expect(record.some(row => row.endsWith(`Record ${id}`))).toBe(false);
      const stepRows = rows.filter(row => /^[✓▶]/.test(row.trimStart()));
      const earlierSteps = rows.find(row => /\+\d+ earlier steps/.test(row))!;
      expect(earlierSteps).toMatch(/^\+\d+ earlier steps$/);
      expect(Number(earlierSteps.match(/\+(\d+)/)?.[1]) + stepRows.length).toBe(8);
      expect(stepRows.at(-1)).toContain("Step 8");
      const stepIds = stepRows.map(row => Number(row.match(/Step (\d+)/)?.[1]));
      expect(stepIds).toEqual(Array.from({ length: stepIds.length }, (_, index) => 9 - stepIds.length + index));
      for (let id = 1; id <= 5; id++) expect(rows.some(row => row.includes(`worker-${id}`))).toBe(true);
    });

    test(`${surface}: multiline step labels stay on one physical row within a 50-row unit pane`, async ($, on) => {
      const data = longUnit();
      data.units[0]!.steps.forEach(step => { step[1] += "\n  detailed\r\n\tstep\tprogress"; });
      const ui = await render($, on, surface, data, true, 76, 50);
      const tree = await ui.drawn();
      const rows = lines(tree);
      const steps = rows.filter(row => /^[✓▶]/.test(row.trimStart()));
      expect(steps.length).toBeGreaterThan(0);
      for (const step of steps) {
        expect(step).not.toMatch(/[\r\n\t]/);
        expect(step).toContain("detailed step progress");
        expect(cellWidth(step)).toBeLessThanOrEqual(76);
      }
      expect(occupiedRows(tree)).toBeLessThanOrEqual(50);
      expect(rows.some(row => /\bopen\b.*Record 10$/.test(row))).toBe(true);
      expect(fitText("  one\n\t two\r\nthree  ", 30)).toBe("  one two three  ");
    });

    for (const bodyRows of [0, 1, 12, 30]) {
      test(`${surface}: unit content respects a ${bodyRows}-row body at 76 columns`, async ($, on) => {
        const ui = await render($, on, surface, longUnit(), true, 76, bodyRows);
        const tree = await ui.drawn();
        expect(occupiedRows(tree)).toBeLessThanOrEqual(bodyRows);
        if (bodyRows >= 12) expect(lines(tree).some(row => /\bopen\b.*Record 10$/.test(row))).toBe(true);
      });
    }

    test(`${surface}: record ids share a column across numeric and as-built phases`, async ($, on) => {
      const data = noPhase();
      data.units[0]!.phases = [phase("9", "done"), phase("as-built", "done")];
      const ui = await render($, on, surface, data, true);
      const rows = lines(await ui.drawn()).filter(row => /Delivery (9|as-built)$/.test(row));
      expect(rows).toHaveLength(2);
      for (const marker of ["0:10", "none stated", "Delivery"]) {
        expect(new Set(rows.map(row => row.indexOf(marker))).size).toBe(1);
      }
    });

    test(`${surface}: finished detail omits empty timing and strip blocks`, async ($, on) => {
      const data = noPhase();
      data.units[0]!.state = "run-finished";
      data.units[0]!.stage = "Run done";
      const ui = await render($, on, surface, data, true);
      const tree = await ui.drawn();
      expect(lines(tree).some(row => row.includes("as of"))).toBe(true);
      expect(emptyBoxes(tree)).toBe(0);
      expect(lines(tree)).toEqual(lines(tree).filter(row => row.trim()));
    });

    test(`${surface}: worker activity columns align after the widest slot role and state`, async ($, on) => {
      const data = noPhase();
      data.units[0]!.workers = [
        { slot: "1", role: "fix", state: "idle", model: "", doing: "short activity" },
        { slot: "adversary", role: "review", state: "running", model: "", doing: "long activity" },
      ];
      const ui = await render($, on, surface, data, true);
      const rows = lines(await ui.drawn()).filter(row => /(?:short|long) activity/.test(row)).map(row => row.trimStart());
      expect(rows).toEqual(["1         fix    idle    short activity", "adversary review running long activity"]);
    });

    test(`${surface}: an empty phase history has an explicit readable record line`, async ($, on) => {
      const data = noPhase();
      data.units[0]!.state = "run-finished";
      const ui = await render($, on, surface, data, true);
      expect(lines(await ui.drawn()).join("\n")).toContain("Phase record");
      const recorded = await ui.findAll({ type: "Text", text: /^no phases recorded$/ });
      expect(recorded).toHaveLength(1);
      expect(recorded.map(item => item.props.color)).toEqual(["inactive"]);
    });

    for (const stage of ["Run done", "Standing by", "Waiting on G2"]) {
      test(`${surface}: no-phase detail header shows only ${stage}`, async ($, on) => {
        const data = noPhase();
        data.units[0]!.stage = stage;
        data.units[0]!.state = stage === "Run done" ? "run-finished" : "standing-by";
        const ui = await render($, on, surface, data, true);
        const rows = lines(await ui.drawn());
        const header = rows.find(row => row.includes("alpha"))!;
        expect(header).toContain(stage);
        expect(header.toLowerCase()).not.toContain("no phase running");
        expect(header).not.toMatch(/·\s*(?:Run done|Standing by|Waiting on)/);
      });
    }

    test(`${surface}: phase record columns align across zero, two and five repair dots`, async ($, on) => {
      const data = noPhase();
      data.units[0]!.phases = [0, 2, 5].map((fixes, index) => {
        const row = phase(String(index + 1), "done");
        row.title = `Record ${index + 1}`;
        row.fix_done = fixes;
        row.fix_running = index === 1 ? 1 : 0;
        return row;
      });
      const ui = await render($, on, surface, data, true);
      const rows = lines(await ui.drawn()).filter(row => /Record \d/.test(row));
      expect(rows).toHaveLength(3);
      for (const row of rows) {
        expect(row).toContain("none stated");
        expect(row).toContain("0:10");
      }
      expect(new Set(rows.map(row => row.indexOf("Record"))).size).toBe(1);
      expect(new Set(rows.map(row => row.indexOf("none stated"))).size).toBe(1);
      expect(new Set(rows.map(row => row.indexOf("0:10"))).size).toBe(1);
      expect(rows.find(row => row.includes("Record 2"))).toContain("●●○");
      expect(rows.find(row => row.includes("Record 3"))).toContain("●●●●●");
    });

    for (const detail of [true]) {
      test(`${surface}: ${detail ? "unit" : "board"} labels past promises and predicted finishes overdue`, async ($, on) => {
        const data = fixture();
        data.units[0]!.eta!.at = time(-12, "17:48");
        data.units[0]!.finish = { kind: "predicted", at: time(-7, "17:53") };
        const ui = await render($, on, surface, data, detail);
        const rows = lines(await ui.drawn());
        expect(rows.find(row => row.startsWith("ETA") || row.trimStart().startsWith("ETA")))
          .toMatch(/ETA 17:48.*overdue 12m/);
        expect(rows.find(row => row.includes("done by 17:53"))).toMatch(/overdue 7m/);
        expect(rows.join("\n")).not.toMatch(/overdue -/);
      });

      test(`${surface}: ${detail ? "unit" : "board"} keeps future forecasts free of overdue labels`, async ($, on) => {
        const ui = await render($, on, surface, fixture(), detail);
        const text = lines(await ui.drawn()).join("\n");
        expect(text).toContain("ETA 18:30");
        expect(text).toContain("done by 18:42");
        expect(text).not.toContain("overdue");
      });
    }

    test(`${surface}: an actual finish in the past is not an overdue forecast`, async ($, on) => {
      const data = noPhase();
      data.units[0]!.state = "run-finished";
      data.units[0]!.stage = "Run done";
      data.units[0]!.finish = { kind: "finished", at: time(-12, "17:48") };
      const ui = await render($, on, surface, data, true);
      const text = lines(await ui.drawn()).join("\n");
      expect(text).toContain("finished 17:48");
      expect(text).not.toContain("overdue");
    });

    test(`${surface}: the active unit names its Plan cell as well as its phase title`, async ($, on) => {
      const data = fixture();
      data.units[0]!.plan = "docs/plans/current-delivery.md";
      const ui = await render($, on, surface, data, true);
      const text = lines(await ui.drawn()).join("\n");
      expect(text).toContain("docs/plans/current-delivery.md");
      expect(text).toContain("Delivery 2");
    });

    test(`${surface}: finished detail displays the resolved as-built path`, async ($, on) => {
      const data = noPhase();
      data.units[0]!.state = "run-finished";
      data.units[0]!.plan = "docs/as-built/delivery.md";
      const ui = await render($, on, surface, data, true);
      expect(lines(await ui.drawn()).join("\n")).toContain("docs/as-built/delivery.md");
    });

    test(`${surface}: an omitted deleted plan adds no empty path row`, async ($, on) => {
      const data = noPhase();
      data.units[0]!.plan = "";
      const ui = await render($, on, surface, data, true);
      const tree = await ui.drawn();
      expect(lines(tree).every(row => row.trim().length > 0)).toBe(true);
      expect(lines(tree).join("\n")).not.toContain("plan.md");
    });
  }
});
