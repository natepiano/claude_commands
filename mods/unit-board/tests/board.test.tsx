import { describe, expect, test } from "claude-code/testing";
import type { Engine } from "claude-code/testing";
import type { On, RenderSurface } from "claude-code";
import { BoardView } from "../hooks/board";
import { UnitView } from "../hooks/unit";
import { cellWidth, fitText } from "../hooks/geometry";
import type { BoardData, BoardUnit } from "../types/board";
import { fixture, NOW, phase, time } from "./kit";

const props = { title: "Units", isFocused: true, bodyColumns: 170,
  scroll: { offset: 0, bodyRows: 50 }, view: {}, placement: "dock" as const };

async function render($: Engine, on: On, surface: RenderSurface, data: BoardData, detail = false, columns = 170, bodyRows = 50) {
  on("ui.render", { component: "Pane", requestId: "phase8-layout" }, ($, event) => {
    const elements = $.ui.resolve(event);
    return detail
      ? UnitView(elements, columns, bodyRows, data, data.units[0]!, NOW / 1000, () => {})
      : BoardView(elements, columns, bodyRows, { files: [], watch: [], loadedAt: NOW / 1000,
        boards: [data] }, NOW / 1000, () => {});
  });
  return $.ui.mount({ plugin: "unit-board", component: "Pane", requestId: "phase8-layout", surface,
    props: { ...props, bodyColumns: columns, scroll: { offset: 0, bodyRows } } });
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
function textSpans(value: unknown): string[] {
  return node(value).type === "Text" ? lines(value) : children(value).flatMap(textSpans);
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

function trunk(): BoardData {
  const data = fixture();
  const running = Array.from({ length: 4 }, (_, index) => {
    const result = unit(`run-${index + 1}`, "running");
    result.phase = { kind: "plan", id: "2", of: 3, title: `Active delivery ${index + 1}` };
    result.stage = ["Build", "Tests", "Review", "Repair round 2"][index]!;
    result.workers = [{ slot: `worker-${index + 1}`, role: "", state: "running", model: "", doing: "" }];
    return result;
  });
  const folded = Array.from({ length: 18 }, (_, index) => {
    const result = unit(`d${String(index + 1).padStart(2, "0")}`, index % 2 ? "standing-by" : "run-finished");
    result.phase = null;
    result.phases = [phase("1", "done")];
    result.stage = index % 2 ? "Standing by" : "Run done";
    result.finish = { kind: "finished", at: time(-10, "17:50") };
    result.workers = [{ slot: "folded-worker", role: "review", state: "paused", model: "", doing: "" }];
    return result;
  });
  data.units = [folded[0]!, running[0]!, folded[1]!, ...running.slice(1), ...folded.slice(2)];
  return data;
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

// Run the same layout assertions against both engine element surfaces.
describe("board layout", () => {
  for (const surface of ["terminal", "desktop"] as const) {
    test(`${surface}: board empty ticket title leaves no title gap at 76 columns`, async ($, on) => {
      const data = fixture();
      const active = data.units[0]!;
      active.phase = { kind: "ticket", id: "names-unit-63", title: "", merged: 62 };
      active.phases = [];
      active.stage = "Build";
      const ui = await render($, on, surface, data, false, 76);
      const tree = await ui.drawn();
      const ticketSpans = textSpans(tree).filter(span => span.includes("Ticket names-unit-63"));

      expect(ticketSpans).toHaveLength(1);
      // The open button may reserve two cells; an absent title adds none.
      expect(ticketSpans[0]).toMatch(/Ticket names-unit-63 · 62 merged {0,2}$/);
      expect(ticketSpans[0]!.match(/names-unit-63/g)).toHaveLength(1);
      expect(cellWidth(lines(tree).find(row => row.includes("Ticket names-unit-63"))!))
        .toBeLessThanOrEqual(76);
    });

    for (const detail of [false, true]) {
      for (const merged of [0, 62]) {
        test(`${surface}: ${detail ? "unit" : "board"} ticket heading shows ${merged} merged at 76 columns`, async ($, on) => {
          const data = fixture();
          const active = data.units[0]!;
          active.phase = { kind: "ticket", id: "alpha-unit-63", title: "Fix names", merged };
          active.phases = [phase("alpha-unit-63", "open")];
          active.stage = "Build";
          const ui = await render($, on, surface, data, detail, 76);
          const rows = lines(await ui.drawn());
          const heading = rows.find(row => row.includes("alpha"))!;

          expect(heading).toContain(detail
            ? `Ticket alpha-unit-63 · ${merged} merged · Build`
            : `  Ticket alpha-unit-63 · ${merged} merged  Fix names`);
          expect(heading).not.toContain("Phase ");
          expect(heading).not.toContain("/1");
          expect(cellWidth(heading)).toBeLessThanOrEqual(76);
        });
      }

      test(`${surface}: ${detail ? "unit" : "board"} plan heading keeps its phase total at 76 columns`, async ($, on) => {
        const data = fixture();
        data.units[0]!.stage = "Build";
        const ui = await render($, on, surface, data, detail, 76);
        const heading = lines(await ui.drawn()).find(row => row.includes("alpha"))!;

        expect(heading).toContain(detail ? "Phase 2 of 3 · Build" : "  Phase 2/3  Delivery 2");
        expect(heading).not.toContain("Ticket ");
        expect(heading).not.toContain("merged");
        expect(cellWidth(heading)).toBeLessThanOrEqual(76);
      });
    }

    test(`${surface}: an empty board gives only the showrunner command instruction`, async ($, on) => {
      on("ui.render", { component: "Pane", requestId: "empty-board" }, ($, event) =>
        BoardView($.ui.resolve(event), 170, 50,
          { files: [], watch: [], loadedAt: NOW / 1000, boards: [] }, NOW / 1000, () => {}));
      const ui = await $.ui.mount({ plugin: "unit-board", component: "Pane", requestId: "empty-board",
        surface, props });

      expect(lines(await ui.drawn()).filter(line => line.trim()))
        .toEqual(["Run /showrunner:unit_board in a showrunner session."]);
      expect(await ui.findAll({ type: "Button" })).toEqual([]);
    });

    for (const bodyRows of [4, 50]) {
      for (const hasStrip of [false, true]) {
      test(`${surface}: a 76-column no-phase unit shows its stage once with ${hasStrip ? "a strip" : "no strip"} in ${bodyRows} rows`, async ($, on) => {
        const data = noPhase();
        const waiting = data.units[0]!;
        waiting.state = "running";
        waiting.stage = "Waiting on G3";
        waiting.phases = hasStrip ? [phase("1", "done"), phase("2", "gated")] : [];
        // Details force the compact branch in the four-row pane.
        waiting.eta = fixture().units[0]!.eta;
        waiting.workers = fixture().units[0]!.workers;
        const ui = await render($, on, surface, data, false, 76, bodyRows);
        const tree = await ui.drawn();
        const rows = lines(tree);
        const header = rows.findIndex(row => row.includes("alpha"));
        expect(rows[header]).toMatch(/alpha\s+Waiting on G3/);
        expect(rows.filter(row => row.includes("Waiting on G3"))).toHaveLength(1);
        expect(rows.filter(row => /[█▓░▒]/.test(row))).toEqual(hasStrip ? ["  █▒"] : []);
        expect(emptyBoxes(tree)).toBe(0);
        expect(occupiedRows(tree)).toBeLessThanOrEqual(bodyRows);
      });
      }
    }

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

    test(`${surface}: excess running units share one whole-key more line inside the body budget`, async ($, on) => {
      const data = trunk();
      const running = Array.from({ length: 25 }, (_, index) => unit(`r${String(index + 1).padStart(2, "0")}`, "running"));
      data.units = [...running, ...data.units.filter(item => !item.phase)];
      const ui = await render($, on, surface, data);
      const tree = await ui.drawn();
      const rows = lines(tree);
      expect(occupiedRows(tree)).toBeLessThanOrEqual(50);
      expect(rows.filter(row => row.startsWith("Done:"))).toHaveLength(1);
      expect(rows.filter(row => row.startsWith("Standing by:"))).toHaveLength(1);
      const more = rows.find(row => /^\+\d+ more running:/.test(row))!;
      expect(more).toBe("+2 more running: r24, r25");
      expect(cellWidth(more)).toBeLessThanOrEqual(170);
      expect(rows.indexOf(more)).toBeLessThan(rows.findIndex(row => row.startsWith("Done:")));
      for (const item of running) expect(rows.filter(row => row.includes(item.key))).toHaveLength(1);
      expect((await ui.findAll({ type: "Button", text: /^open$/i })).length).toBe(23);
    });

    test(`${surface}: a narrow compact row keeps stage and overdue ETA ahead of the strip`, async ($, on) => {
      const data = fixture();
      const active = data.units[0]!;
      active.stage = "Build";
      active.eta!.at = time(-12, "17:48");
      active.phases = Array.from({ length: 30 }, (_, index) => phase(String(index + 1), "done"));
      const ui = await render($, on, surface, data, false, 40, 3);
      const tree = await ui.drawn();
      expect(occupiedRows(tree)).toBeLessThanOrEqual(3);
      const compact = lines(tree).find(row => row.includes("ETA 17:48"))!;
      const visible = fitText(compact, 40);
      expect(visible).toContain("Build  ETA 17:48 · overdue 12m");
      expect(visible.indexOf("Build")).toBeLessThan(visible.indexOf("ETA"));
      expect(compact.indexOf("overdue 12m")).toBeLessThan(compact.indexOf("█"));
      expect(cellWidth(visible)).toBeLessThanOrEqual(40);
    });

    for (const bodyRows of [0, 1, 2, 3]) {
      test(`${surface}: a ${bodyRows}-row body reserves folds before headings and overflow notices`, async ($, on) => {
        const ui = await render($, on, surface, trunk(), false, 40, bodyRows);
        const tree = await ui.drawn();
        const rows = lines(tree);
        expect(occupiedRows(tree)).toBeLessThanOrEqual(bodyRows);
        if (bodyRows >= 1) expect(rows.some(row => row.startsWith("Done:"))).toBe(true);
        if (bodyRows >= 2) expect(rows.some(row => row.startsWith("Standing by:"))).toBe(true);
      });
    }

    test(`${surface}: all 22 trunk units fit with separate ordered finished and standing-by lines`, async ($, on) => {
      const data = trunk();
      const ui = await render($, on, surface, data);
      const tree = await ui.drawn();
      const rows = lines(tree);
      const folded = data.units.filter(item => item.phase === null && item.state === "run-finished");
      const doneRows = rows.filter(row => row.trimStart().startsWith("Done:"));
      expect(doneRows.map(row => row.trim())).toEqual([`Done: ${folded.map(item => item.key).join(", ")}`]);
      expect(rows.filter(row => row.startsWith("Standing by:"))).toEqual([
        `Standing by: ${data.units.filter(item => item.state === "standing-by").map(item => item.key).join(", ")}`,
      ]);
      expect(rows.findIndex(row => row.startsWith("Standing by:"))).toBeGreaterThan(rows.indexOf(doneRows[0]!));
      for (const item of data.units) expect(rows.join("\n")).toContain(item.key);
      expect(rows.filter(row => /●.*run-\d/.test(row)).map(row => row.match(/run-\d/)?.[0]))
        .toEqual(["run-1", "run-2", "run-3", "run-4"]);
      for (const item of data.units.filter(item => item.phase !== null)) {
        const header = rows.findIndex(row => row.includes(item.key));
        expect(rows[header]).toContain(item.phase!.title);
        const block = rows.slice(header, header + 7).join("\n");
        expect(block).toContain(item.stage);
        expect(block).toContain("ETA 18:30");
        expect(block).toContain("done by 18:42");
        expect(block).toContain(item.workers[0]!.slot);
        expect(block).toContain("│");
      }
      expect(rows.indexOf(doneRows[0]!)).toBeGreaterThan(rows.findIndex(row => row.includes("worker-4")));
      expect(rows.join("\n")).not.toContain("folded-worker");
      expect(occupiedRows(tree)).toBeLessThanOrEqual(50);
    });

    test(`${surface}: narrow fold lines each end with an accurate overflow count`, async ($, on) => {
      const data = trunk();
      const ui = await render($, on, surface, data, false, 40);
      const rows = lines(await ui.drawn());
      const doneRows = rows.filter(row => row.trimStart().startsWith("Done:"));
      expect(doneRows).toHaveLength(1);
      const row = doneRows[0]!.trim();
      expect(row.length).toBeLessThanOrEqual(40);
      expect(row).toMatch(/\+\d+ more$/);
      const shown = row.match(/d\d{2}/g) ?? [];
      const omitted = Number(row.match(/\+(\d+) more$/)?.[1]);
      const foldedKeys = data.units.filter(item => item.phase === null && item.state === "run-finished").map(item => item.key);
      expect(shown.length).toBeGreaterThan(0);
      expect(shown).toEqual(foldedKeys.slice(0, shown.length));
      expect(shown.length + omitted).toBe(foldedKeys.length);
      const standing = rows.find(row => row.startsWith("Standing by:"))!;
      expect(standing.length).toBeLessThanOrEqual(40);
      expect(standing).toMatch(/\+\d+ more$/);
      const idleKeys = data.units.filter(item => item.state === "standing-by").map(item => item.key);
      const idleShown = standing.match(/d\d{2}/g) ?? [];
      expect(idleShown).toEqual(idleKeys.slice(0, idleShown.length));
      expect(idleShown.length + Number(standing.match(/\+(\d+) more$/)?.[1])).toBe(idleKeys.length);
      for (let index = 1; index <= 4; index++) expect(rows.join("\n")).toContain(`worker-${index}`);
    });

    test(`${surface}: a gate wait keeps a single header below running units and above folds`, async ($, on) => {
      const data = trunk();
      const waiting = { ...noPhase().units[0]!, key: "waiting-unit", state: "standing-by" as const, stage: "Waiting on G2",
        phases: [phase("3", "gated")] };
      waiting.workers = [{ slot: "hidden-waiter", role: "", state: "paused", model: "", doing: "" }];
      data.units.unshift(waiting);
      const ui = await render($, on, surface, data);
      const rows = lines(await ui.drawn());
      const header = rows.find(row => row.includes("waiting-unit"))!;
      expect(header).toContain("Waiting on G2");
      expect(rows.filter(row => row.includes("Waiting on G2"))).toHaveLength(1);
      expect(rows.find(row => row.startsWith("Done:"))).not.toContain("waiting-unit");
      expect(rows.find(row => row.startsWith("Standing by:"))).not.toContain("waiting-unit");
      expect(rows.join("\n")).not.toContain("hidden-waiter");
      expect(rows.indexOf(header)).toBeGreaterThan(rows.findIndex(row => row.includes("worker-4")));
      expect(rows.indexOf(header)).toBeLessThan(rows.findIndex(row => row.startsWith("Done:")));
    });

    for (const bodyRows of [50, 20]) {
      test(`${surface}: seven running units and both fold lines stay inside ${bodyRows} body rows`, async ($, on) => {
        const data = trunk();
        const folded = data.units.filter(item => !item.phase).slice(0, 15);
        const running = Array.from({ length: 7 }, (_, index) => {
          const result = unit(`active-${index + 1}`, "running");
          result.workers = [{ slot: `details-${index + 1}`, role: "", state: "running", model: "", doing: "" }];
          result.eta!.at = time(-12, "17:48");
          return result;
        });
        data.units = [...running, ...folded];
        const ui = await render($, on, surface, data, false, 170, bodyRows);
        const tree = await ui.drawn();
        const rows = lines(tree);
        expect(occupiedRows(tree)).toBeLessThanOrEqual(bodyRows);
        for (const item of running) expect(rows.filter(row => row.includes(item.key))).toHaveLength(1);
        expect(rows.filter(row => row.startsWith("Done:"))).toHaveLength(1);
        expect(rows.filter(row => row.startsWith("Standing by:"))).toHaveLength(1);
        expect((await ui.findAll({ type: "Button", text: /^open$/i })).length).toBe(7);
        const lastHeader = rows.findIndex(row => row.includes("active-7"));
        expect(rows[lastHeader + 1]).toMatch(/Repair round 2.*ETA 17:48.*overdue 12m/);
        expect(rows[lastHeader + 2]).toMatch(/^Done:/);
        expect(rows.join("\n")).not.toContain("details-7");
        if (bodyRows === 50) expect(rows.join("\n")).toContain("details-1");
        else expect(rows.join("\n")).not.toContain("details-1");
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

    test(`${surface}: no-phase stage appears once in the header while phased stage stays with the strip`, async ($, on) => {
      const data = noPhase();
      data.units[0]!.state = "running";
      data.units[0]!.stage = "Waiting on G2";
      data.units.push(unit("phased", "running"));
      const ui = await render($, on, surface, data);
      const rows = lines(await ui.drawn());
      expect(rows.filter(row => row.includes("Waiting on G2"))).toHaveLength(1);
      expect(rows.find(row => row.includes("alpha"))).toMatch(/●\s+alpha\s+Waiting on G2/);
      expect(rows.join("\n").toLowerCase()).not.toContain("no phase running");
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

    test(`${surface}: missing worker roles use slot names and ended workers are hidden`, async ($, on) => {
      const data = noPhase();
      data.units[0]!.state = "running";
      data.units[0]!.workers = ["impl", "test"].map(slot => ({ slot, role: "", state: "paused", model: "", doing: "" }));
      const ended = unit("closed", "running");
      ended.workers = [{ slot: "1", role: "review", state: "ended", model: "", doing: "" }];
      data.units.push(ended);
      const ui = await render($, on, surface, data);
      const rows = lines(await ui.drawn());
      const workers = rows.find(row => row.includes("paused"))!;
      expect(workers).toContain("impl paused · test paused");
      expect(workers).not.toMatch(/\b2 paused\b/);
      expect(rows.join("\n")).not.toContain("ended");
    });

    test(`${surface}: a no-phase predicted finish requires a later todo or gated phase`, async ($, on) => {
      const data = noPhase();
      const base = data.units[0]!;
      base.state = "running";
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

    test(`${surface}: a long phase title leaves an ellipsis and two spaces before open`, async ($, on) => {
      const data = fixture();
      data.units[0]!.phase!.title = "A phase title that deliberately exceeds the entire narrow pane width";
      const ui = await render($, on, surface, data, false, 60);
      const header = lines(await ui.drawn()).find(row => row.includes("alpha"))!;
      expect(header.length).toBeLessThanOrEqual(60);
      expect(header).toMatch(/… {2,}open$/);
      expect((await ui.findAll({ type: "Button", text: /^open$/i })).length).toBe(1);
    });

    for (const detail of [false, true]) {
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
