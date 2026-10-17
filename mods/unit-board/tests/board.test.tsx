import { describe, expect, test } from "claude-code/testing";
import type { Engine } from "claude-code/testing";
import type { On, RenderSurface } from "claude-code";
import { BoardView } from "../hooks/board";
import { UnitView } from "../hooks/unit";
import type { BoardData, BoardUnit } from "../types/board";
import { fixture, NOW, phase, time } from "./kit";

const props = { title: "Units", isFocused: true, bodyColumns: 170,
  scroll: { offset: 0, bodyRows: 50 }, view: {}, placement: "dock" as const };

async function render($: Engine, on: On, surface: RenderSurface, data: BoardData, detail = false) {
  on("ui.render", { component: "Pane", requestId: "phase8-layout" }, ($, event) => {
    const elements = $.ui.resolve(event);
    return detail
      ? UnitView(elements, props.bodyColumns, data, data.units[0]!, NOW / 1000, () => {})
      : BoardView(elements, props.bodyColumns, { files: [], watch: [], loadedAt: NOW / 1000,
        boards: [data], focus: null }, NOW / 1000, () => {});
  });
  return $.ui.mount({ plugin: "unit-board", component: "Pane", requestId: "phase8-layout", surface, props });
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

// Run the same layout assertions against both engine element surfaces.
describe("phase 8 board layout", () => {
  for (const surface of ["terminal", "desktop"] as const) {
    test(`${surface}: running and standing-by units precede compact finished rows in stable order`, async ($, on) => {
      const data = fixture();
      const done = Array.from({ length: 20 }, (_, index) => {
        const result = unit(`done-${index}`, "run-finished");
        result.stage = "Run done";
        result.finish = index === 0 ? { kind: "unknown" } : { kind: "finished", at: time(-10, "17:50") };
        result.phase = null;
        result.phases = [phase("1", "done")];
        result.workers = [{ slot: "hidden", role: "review", state: "running", model: "", doing: "" }];
        return result;
      });
      data.units = [done[0]!, unit("standby", "standing-by"), done[1]!, unit("active", "running"), ...done.slice(2)];
      const ui = await render($, on, surface, data);
      const rows = lines(await ui.drawn());
      const headers = rows.filter(row => /●.*(?:done-\d+|standby|active)/.test(row));
      const order = headers.map(row => row.match(/done-\d+|standby|active/)?.[0]);
      expect(order).toEqual(["active", "standby", ...done.map(item => item.key)]);
      expect(headers.slice(2).every(row => /Run done.*open/i.test(row))).toBe(true);
      expect(headers[2]).not.toContain("17:50");
      expect(headers.slice(3).every(row => row.includes("17:50"))).toBe(true);
      expect(rows.filter(row => row.includes("hidden") || row.includes("review running"))).toEqual([]);
      expect((await ui.findAll({ type: "Button", text: /^open$/i })).length).toBe(22);
      const finishedHeaders = (await ui.findAll({ type: "Box" })).filter(box =>
        /done-\d+/.test(box.text) && box.children.some(child => node(child).type === "Button"));
      expect(finishedHeaders).toHaveLength(20);
      expect(finishedHeaders.every(box => box.props.justifyContent === "space-between"
        || box.children.slice(0, -1).some(child => Number(node(node(child).props).flexGrow ?? 0) > 0))).toBe(true);
      expect(rows.length).toBeLessThanOrEqual(50);
    });

    test(`${surface}: no-phase stage appears once in the header while phased stage stays with the strip`, async ($, on) => {
      const data = noPhase();
      data.units[0]!.stage = "Waiting on G2";
      data.units.push(unit("phased", "running"));
      const ui = await render($, on, surface, data);
      const rows = lines(await ui.drawn());
      expect(rows.filter(row => row.includes("Waiting on G2"))).toHaveLength(1);
      expect(rows.find(row => row.includes("alpha"))).toMatch(/●\s+alpha\s+Waiting on G2/);
      expect(rows.join("\n")).not.toContain("no phase running");
      expect(rows.find(row => row.includes("phased"))).toMatch(/Phase 2\/3.*Delivery 2/);
      expect(rows.find(row => row.includes("Repair round 2"))).not.toContain("phased");
    });

    test(`${surface}: strip stage separator appears only after actual cells`, async ($, on) => {
      const data = noPhase();
      const empty = unit("empty-strip", "running");
      empty.phases = [];
      empty.stage = "Empty strip stage";
      const filled = unit("filled-strip", "running");
      filled.stage = "Filled strip stage";
      data.units = [empty, filled];
      const ui = await render($, on, surface, data);
      const rows = lines(await ui.drawn());
      const emptyRow = rows.find(row => row.includes("Empty strip stage"))!;
      const filledRow = rows.find(row => row.includes("Filled strip stage"))!;
      expect(emptyRow).toBe("  Empty strip stage");
      expect(filledRow).toMatch(/[█▓░▒]  Filled strip stage/);
      expect(rows.filter(row => /^ {4}/.test(row))).toEqual([]);
    });

    test(`${surface}: empty worker roles use single spaces and ended workers are hidden`, async ($, on) => {
      const data = noPhase();
      data.units[0]!.workers = ["1", "2"].map(slot => ({ slot, role: "", state: "paused", model: "", doing: "" }));
      const ended = unit("closed", "running");
      ended.workers = [{ slot: "1", role: "review", state: "ended", model: "", doing: "" }];
      data.units.push(ended);
      const ui = await render($, on, surface, data);
      const rows = lines(await ui.drawn());
      expect(rows.filter(row => row.includes("paused")).map(row => row.trim())).toEqual(["workers  2 paused"]);
      expect(rows.join("\n")).not.toContain("ended");
    });

    test(`${surface}: a no-phase predicted finish requires a later todo or gated phase`, async ($, on) => {
      const data = noPhase();
      const base = data.units[0]!;
      base.finish = { kind: "predicted", at: time(42, "18:42") };
      base.phases = [phase("1", "done")];
      const todo = { ...base, key: "future", phases: [phase("2", "todo")],
        finish: { kind: "predicted" as const, at: time(60, "19:00") } };
      const gated = { ...base, key: "gated", phases: [phase("3", "gated")],
        finish: { kind: "predicted" as const, at: time(90, "19:30") } };
      data.units.push(todo, gated);
      const ui = await render($, on, surface, data);
      const text = lines(await ui.drawn()).join("\n");
      expect(text).not.toContain("18:42");
      expect(text).toContain("done by 19:00");
      expect(text).toContain("done by 19:30");
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

    test(`${surface}: an empty phase history has an explicit subtle record line`, async ($, on) => {
      const data = noPhase();
      data.units[0]!.state = "run-finished";
      const ui = await render($, on, surface, data, true);
      expect(lines(await ui.drawn()).join("\n")).toContain("Phase record");
      const recorded = await ui.findAll({ type: "Text", text: /^no phases recorded$/ });
      expect(recorded.map(item => item.props.color)).toEqual(["subtle"]);
    });
  }
});
